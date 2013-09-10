#!/usr/bin/env python

#tooltool is a lookaside cache implemented in Python
#Copyright (C) 2011 John H. Ford <john@johnford.info>
#
#This program is free software; you can redistribute it and/or
#modify it under the terms of the GNU General Public License
#as published by the Free Software Foundation version 2
#
#This program is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU General Public License for more details.
#
#You should have received a copy of the GNU General Public License
#along with this program; if not, write to the Free Software
#Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

# An manifest file specifies files in that directory that are stored
# elsewhere.  This file should only contain file in the directory
# which the manifest file resides in and it should be called 'manifest.manifest'

__version__ = '1'

import os
import optparse
import logging
import hashlib
import urllib2
import shutil
import sys

DEFAULT_MANIFEST_NAME = 'manifest.tt'
TEMP_SUFFIX=".temp"

try:
    import simplejson as json  # I hear simplejson is faster
except ImportError:
    import json

log = logging.getLogger(__name__)


class FileRecordJSONEncoderException(Exception):
    pass


class InvalidManifest(Exception):
    pass


class ExceptionWithFilename(Exception):

    def __init__(self, filename):
        Exception.__init__(self)
        self.filename = filename


class DigestMismatchException(ExceptionWithFilename):
    pass


class MissingFileException(ExceptionWithFilename):
    pass


class FileRecord(object):

    def __init__(self, filename, size, digest, algorithm):
        object.__init__(self)
        self.filename = filename
        self.size = size
        self.digest = digest
        self.algorithm = algorithm
        log.debug("creating %s 0x%x" % (self.__class__.__name__, id(self)))

    def __eq__(self, other):
        if self is other:
            return True
        if self.filename == other.filename and \
           self.size == other.size and \
           self.digest == other.digest and \
           self.algorithm == other.algorithm:
            return True
        else:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return repr(self)

    def __repr__(self):
        return "%s.%s(filename='%s', size='%s', digest='%s', algorithm='%s')" % (__name__,
                                                                                 self.__class__.__name__,
                                                                                 self.filename,
                                                                                 self.size,
                                                                                 self.digest, self.algorithm)

    def present(self):
        # Doesn't check validity
        return os.path.exists(self.filename)

    def validate_size(self):
        if self.present():
            return self.size == os.path.getsize(self.filename)
        else:
            log.debug("trying to validate size on a missing file, %s", % self.filename)
            raise MissingFileException(filename=self.filename)

    def validate_digest(self):
        if self.present():
            with open(self.filename, 'rb') as f:
                return self.digest == digest_file(f, self.algorithm)
        else:
            log.debug("trying to validate digest on a missing file, %s", % self.filename)
            raise MissingFileException(filename=self.filename)

    def validate(self):
        if self.validate_size():
            if self.validate_digest():
                return True
        return False

    def describe(self):
        if self.present() and self.validate():
            return "'%s' is present and valid" % self.filename
        elif self.present():
            return "'%s' is present and invalid" % self.filename
        else:
            return "'%s' is absent" % self.filename


def create_file_record(filename, algorithm):
    fo = open(filename, 'rb')
    stored_filename = os.path.split(filename)[1]
    fr = FileRecord(stored_filename, os.path.getsize(filename), digest_file(fo, algorithm), algorithm)
    fo.close()
    return fr


class FileRecordJSONEncoder(json.JSONEncoder):

    def encode_file_record(self, obj):
        if not issubclass(type(obj), FileRecord):
            err = "FileRecordJSONEncoder is only for FileRecord and lists of FileRecords, not %s" % obj.__class__.__name__
            log.warn(err)
            raise FileRecordJSONEncoderException(err)
        else:
            return {'filename': obj.filename, 'size': obj.size, 'algorithm': obj.algorithm, 'digest': obj.digest}

    def default(self, f):
        if issubclass(type(f), list):
            record_list = []
            for i in f:
                record_list.append(self.encode_file_record(i))
            return record_list
        else:
            return self.encode_file_record(f)


class FileRecordJSONDecoder(json.JSONDecoder):
    """I help the json module materialize a FileRecord from
    a JSON file.  I understand FileRecords and lists of
    FileRecords.  I ignore things that I don't expect for now"""
    # TODO: make this more explicit in what it's looking for
    # and error out on unexpected things

    def process_file_records(self, obj):
        if isinstance(obj, list):
            record_list = []
            for i in obj:
                record = self.process_file_records(i)
                if issubclass(type(record), FileRecord):
                    record_list.append(record)
            return record_list
        if isinstance(obj, dict) and \
           len(obj.keys()) == 4 and \
           'filename' in obj and \
           'size' in obj and \
           'algorithm' in obj and \
           'digest' in obj:
            rv = FileRecord(obj['filename'], obj['size'], obj['digest'], obj['algorithm'])
            log.debug("materialized %s" % rv)
            return rv
        return obj

    def decode(self, s):
        decoded = json.JSONDecoder.decode(self, s)
        rv = self.process_file_records(decoded)
        return rv


class Manifest(object):

    valid_formats = ('json',)

    def __init__(self, file_records=None):
        self.file_records = file_records or []

    def __eq__(self, other):
        if self is other:
            return True
        if len(self.file_records) != len(other.file_records):
            log.debug('Manifests differ in number of files')
            return False
        #TODO: Lists in a different order should be equal
        for record in range(0, len(self.file_records)):
            if self.file_records[record] != other.file_records[record]:
                log.debug('FileRecords differ, %s vs %s' % (self.file_records[record],
                                                            other.file_records[record]))
                return False
        return True

    def __deepcopy__(self, memo):
        # This is required for a deep copy
        return Manifest(self.file_records[:])

    def __copy__(self):
        return Manifest(self.file_records)

    def copy(self):
        return Manifest(self.file_records[:])

    def present(self):
        return all(i.present() for i in self.file_records)

    def validate_sizes(self):
        return all(i.validate_size() for i in self.file_records)

    def validate_digests(self):
        return all(i.validate_digest() for i in self.file_records)

    def validate(self):
        return all(i.validate() for i in self.file_records)

    def sort(self):
        #TODO: WRITE TESTS
        self.file_records.sort(key=lambda x: x.size)

    def load(self, data_file, fmt='json'):
        assert fmt in self.valid_formats
        if fmt == 'json':
            try:
                self.file_records.extend(json.load(data_file, cls=FileRecordJSONDecoder))
                self.sort()
            except ValueError:
                raise InvalidManifest("trying to read invalid manifest file")

    def loads(self, data_string, fmt='json'):
        assert fmt in self.valid_formats
        if fmt == 'json':
            try:
                self.file_records.extend(json.loads(data_string, cls=FileRecordJSONDecoder))
                self.sort()
            except ValueError:
                raise InvalidManifest("trying to read invalid manifest file")

    def dump(self, output_file, fmt='json'):
        assert fmt in self.valid_formats
        self.sort()
        if fmt == 'json':
            rv = json.dump(self.file_records, output_file, indent=0, cls=FileRecordJSONEncoder)
            print >> output_file, ''
            return rv

    def dumps(self, fmt='json'):
        assert fmt in self.valid_formats
        self.sort()
        if fmt == 'json':
            return json.dumps(self.file_records, cls=FileRecordJSONEncoder)


def digest_file(f, a):
    """I take a file like object 'f' and return a hex-string containing
    of the result of the algorithm 'a' applied to 'f'."""
    h = hashlib.new(a)
    chunk_size = 1024 * 10
    data = f.read(chunk_size)
    while data:
        h.update(data)
        data = f.read(chunk_size)
    if hasattr(f, 'name'):
        log.debug('hashed %s with %s to be %s', f.name, a, h.hexdigest())
    else:
        log.debug('hashed a file with %s to be %s', a, h.hexdigest())
    return h.hexdigest()


# TODO: write tests for this function
def open_manifest(manifest_file):
    """I know how to take a filename and load it into a Manifest object"""
    if os.path.exists(manifest_file):
        manifest = Manifest()
        with open(manifest_file) as f:
            manifest.load(f)
            log.debug("loaded manifest from file '%s'" % manifest_file)
        return manifest
    else:
        log.debug("tried to load absent file '%s' as manifest" % manifest_file)
        raise InvalidManifest("manifest file '%s' does not exist" % manifest_file)


# TODO: write tests for this function
def list_manifest(manifest_file):
    """I know how print all the files in a location"""
    try:
        manifest = open_manifest(manifest_file)
    except InvalidManifest:
        log.error("failed to load manifest file at '%s'" % manifest_file)
        return False
    for f in manifest.file_records:
        print "%s\t%s\t%s" % ("P" if f.present() else "-",
                              "V" if f.present() and f.validate() else "-",
                              f.filename)
    return True


def validate_manifest(manifest_file):
    """I validate that all files in a manifest are present and valid but
    don't fetch or delete them if they aren't"""
    try:
        manifest = open_manifest(manifest_file)
    except InvalidManifest:
        log.error("failed to load manifest file at '%s'" % manifest_file)
        return False
    invalid_files = []
    absent_files = []
    for f in manifest.file_records:
        if not f.present():
            absent_files.append(f)
        else:
            if not f.validate():
                invalid_files.append(f)
    if len(invalid_files + absent_files) == 0:
        return True
    else:
        return False


# TODO: write tests for this function
def add_files(manifest_file, algorithm, filenames, create_package=False):
    # returns True if all files successfully added, False if not
    # and doesn't catch library Exceptions.  If any files are already
    # tracked in the manifest, return will be False because they weren't
    # added
    all_files_added = True
    # Create a old_manifest object to add to
    if os.path.exists(manifest_file):
        old_manifest = open_manifest(manifest_file)
    else:
        old_manifest = Manifest()
        log.debug("creating a new manifest file")
    new_manifest = Manifest()  # use a different manifest for the output
    for filename in filenames:
        log.debug("adding %s" % filename)
        path, name = os.path.split(filename)
        new_fr = create_file_record(filename, algorithm)
        if create_package:
            shutil.copy(filename,
                        os.path.join(os.path.split(manifest_file)[0], new_fr.digest))
            log.debug("Added file %s to tooltool package %s with hash %s" % (filename, os.path.split(manifest_file)[0], new_fr.digest))
        log.debug("appending a new file record to manifest file")
        add = True
        for fr in old_manifest.file_records:
            log.debug("manifest file has '%s'" % "', ".join([x.filename for x in old_manifest.file_records]))
            if new_fr == fr and new_fr.validate():
                # TODO: Decide if this case should really cause a False return
                log.info("file already in old_manifest file and matches")
                add = False
            elif new_fr == fr and not new_fr.validate():
                log.error("file already in old_manifest file but is invalid")
                add = False
            if filename == fr.filename:
                log.error("manifest already contains file named %s" % filename)
                add = False
        if add:
            new_manifest.file_records.append(new_fr)
            log.debug("added '%s' to manifest" % filename)
        else:
            all_files_added = False
    with open(manifest_file, 'wb') as output:
        new_manifest.dump(output, fmt='json')
    return all_files_added


def touch(f):
    """Used to modify mtime in cached files;
    mtime is used by the purge command"""
    try:
        os.utime(f, None)
    except OSError:
        log.warn('impossible to update utime of file %s' % f)


# TODO: write tests for this function
def fetch_file(base_urls, file_record, grabchunk=1024 * 4, cache_folder=None, temp_suffix=''):
    # A file which is requested to be fetched that exists locally will be overwritten by this function


    #case 2: fetch the file
    fetched = False
    for base_url in base_urls:
        # Generate the URL for the file on the server side
        url = "%s/%s/%s" % (base_url, file_record.algorithm, file_record.digest)

        log.info("Attempting to fetch from '%s'..." % base_url)

        # TODO: This should be abstracted to make generic retrival protocol handling easy
        # Well, the file doesn't exist locally.  Let's fetch it.
        try:
            f = urllib2.urlopen(url)
            log.debug("opened %s for reading" % url)
            with open("%s%s" % (file_record.filename, temp_suffix), 'wb') as out:
                k = True
                size = 0
                while k:
                    # TODO: print statistics as file transfers happen both for info and to stop
                    # buildbot timeouts
                    indata = f.read(grabchunk)
                    out.write(indata)
                    size += len(indata)
                    if indata == '':
                        k = False
                log.info("File %s%s fetched from %s" % (file_record.filename, temp_suffix, base_url))
                fetched = True
        except (urllib2.URLError, urllib2.HTTPError, ValueError) as e:
            log.info("...failed to fetch '%s' from %s" % (file_record.filename, base_url))
            log.debug("%s" % e)
        except IOError:
            log.info("failed to write to '%s'" % file_record.filename, exc_info=True)


    return fetched


# TODO: write tests for this function
def fetch_files(manifest_file, base_urls, overwrite, filenames=[], cache_folder=None):
    # Lets load the manifest file
    try:
        manifest = open_manifest(manifest_file)
    except InvalidManifest:
        log.error("failed to load manifest file at '%s'" % manifest_file)
        return False


    # we want to track files already in current working directory AND valid
    # we will not need to fetch these
    present_files=[]
    
    # We want to track files that fail to be fetched as well as
    # files that are fetched
    failed_files = []
    fetched_files = []
    
    # Lets go through the manifest and fetch the files that we want
    for f in manifest.file_records:
        # case 1: files are already present
        if f.present():
            if f.validate():
                present_files.append(f.filename)
            else:
                # we have an invalid file here, better to cleanup!
                # this invalid file needs to be replaced with a good one
                # from the local cash or fetched from a tooltool server
                os.remove(os.path.join(os.getcwd(), f.filename))

        # check if file is already in cache
        if cache_folder and f.filename not in present_files:
            try:
                shutil.copy(os.path.join(cache_folder, f.digest),
                            os.path.join(os.getcwd(), f.filename  ))
                log.info("File %s retrieved from local cache %s" %
                         (f.filename, cache_folder))
                touch(os.path.join(cache_folder, f.digest))
                # the file is now present and, since it has been taken from cache, is certainly valid
                # because no object is ever copied into the local cache without validation
                present_files.append(f.filename)
                return True
            except IOError:
                log.info("File %s not present in local cache folder %s" %
                         (f.filename, cache_folder))

        # now I will try to fetch all files which are not already present and valid, appending a suffix to avoid race conditions
        if (f.filename in filenames or len(filenames) == 0) and f.filename not in present_files:
            log.debug("fetching %s" % f.filename)
            if fetch_file(base_urls, f, cache_folder=cache_folder, temp_suffix=TEMP_SUFFIX):
                fetched_files.append(f)
            else:
                failed_files.append(f.filename)
        else:
            log.debug("skipping %s" % f.filename)

    # lets ensure that fetched files match what the manifest specified
    for localfile in fetched_files:
        # since I appended a temp suffix while downloading, I need to perform all validations on the file with the temp suffix
        # this is why filerecord_for_validation is created

        filerecord_for_validation = FileRecord( "%s%s" % (localfile.filename,TEMP_SUFFIX), localfile.size, localfile.digest, localfile.algorithm)

        if filerecord_for_validation.validate():
            # great!
            # I can remove the temporary suffix
            os.rename(os.path.join(os.getcwd(), filerecord_for_validation.filename), os.path.join(os.getcwd(), localfile.filename))
            
            # if I am using a cache and a new file has just been retrieved from a
            # remote location, I need to update the cache as well
            if cache_folder:
                log.info("Updating local cache %s..." % cache_folder)
                try:
                    if not os.path.exists(cache_folder):
                        log.info("Creating cache in %s..." % cache_folder)
                        os.makedirs(cache_folder, 0700)
                    shutil.copy(os.path.join(os.getcwd(), localfile.filename),
                                os.path.join(cache_folder, localfile.digest))
                    log.info("Local cache %s updated with %s" % (cache_folder,
                                                                 localfile.filename))
                    touch(os.path.join(cache_folder, localfile.digest))
                except (OSError, IOError):
                    log.warning('Impossible to add file %s to cache folder %s' %
                                (localfile.filename, cache_folder), exc_info=True)
        else:
            failed_files.append(localfile.filename)
            log.error("'%s'" % filerecord_for_validation.describe(TEMP_SUFFIX))

    # If we failed to fetch or validate a file, we need to fail
    if len(failed_files) > 0:
        log.error("The following files failed: '%s'" % "', ".join(failed_files))
        return False
    return True


def freespace(p):
    "Returns the number of bytes free under directory `p`"
    if sys.platform == 'win32':
        # os.statvfs doesn't work on Windows
        import win32file

        secsPerClus, bytesPerSec, nFreeClus, totClus = win32file.GetDiskFreeSpace(p)
        return secsPerClus * bytesPerSec * nFreeClus
    else:
        r = os.statvfs(p)
        return r.f_frsize * r.f_bavail


def remove(absolute_file_path):
    try:
        os.remove(absolute_file_path)
    except OSError:
        log.info("Impossible to remove %s" % absolute_file_path, exc_info=True)


def purge(folder, gigs):
    """If gigs is non 0, it deletes files in `folder` until `gigs` GB are free, starting from older files.
    If gigs is 0, a full purge will be performed.
    No recursive deletion of files in subfolder is performed."""

    full_purge = bool(gigs == 0)
    gigs *= 1024 * 1024 * 1024

    if not full_purge and freespace(folder) >= gigs:
        log.info("No need to cleanup")
        return

    files = []
    try:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            # it deletes files in folder without going into subfolders,
            # assuming the cache has a flat structure
            if not os.path.isfile(p):
                continue
            mtime = os.path.getmtime(p)
            files.append((mtime, p))
    except OSError:
        log.info('Impossible to list content of folder %s' % folder,
                 exc_info=True)
        return

    # iterate files sorted by mtime
    for _, f in sorted(files):
        log.info("removing %s to free up space" % f)
        remove(f)
        if not full_purge and freespace(folder) >= gigs:
            break


def package(folder, algorithm, message):
    if not os.path.exists(folder) or not os.path.isdir(folder):
        log.error('Folder %s does not exist!' % folder)
        return

    from os import walk

    dirname, basename = os.path.split(folder)

    filenames = []
    for (_dirpath, _dirnames, files) in walk(folder):
        filenames.extend(files)
        break  # not to navigate subfolders

    default_package_name = basename + '.TOOLTOOL-PACKAGE'

    package_name = default_package_name
    manifest_name = basename + '.tt'
    notes_name = basename + '.txt'

    suffix = 1
    while os.path.exists(os.path.join(dirname, package_name)):
        package_name = default_package_name + str(suffix)
        manifest_name = basename + str(suffix) +'.tt'
        notes_name = basename + str(suffix) + '.txt'
        suffix = suffix + 1

    os.makedirs(os.path.join(dirname, package_name))

    log.info("Creating package %s from folder %s..." % (os.path.join(os.path.join(dirname, package_name)), folder))

    add_files(os.path.join(os.path.join(dirname, package_name), manifest_name), algorithm, [os.path.join(folder, x) for x in filenames], create_package=True)

    try:
        f = open(os.path.join(os.path.join(dirname, package_name), notes_name)  , 'wb')
        try:
            f.write(message) # Write a string to a file
        finally:
            f.close()
    except IOError:
        pass

    log.info("Package %s has been created from folder %s" % (os.path.join(os.path.join(dirname, package_name)), folder))

    return os.path.join(os.path.join(dirname, package_name))

from subprocess import Popen, PIPE
def execute(cmd):
    process = Popen(cmd, shell=True, stdout=PIPE)
    while True:
        line = process.stdout.readline()
        if not line: break
        log.info(line.replace('\n', ''))
        

def upload(package, user, host, path):
    #TODO s: validate package
    cmd1 = "rsync  %s/* %s@%s:%s --progress -f '- *.tt' -f '- *.txt'" % ( package, user, host, path) 
    
    cmd2 = "rsync  %s/* %s@%s:%s --progress -f '+ *.tt' -f '+ *.txt'" % ( package, user, host, path) 

    log.info("The following two rsync commands will be executed to transfer the tooltool package:")
    log.info("1) %s"%cmd1)
    log.info("2) %s"%cmd2)
    log.info("Please note that the order of execution IS relevant!")
    log.info("Uploading hashed files with command: %s" % cmd1)
    execute(cmd1)
    log.info("Uploading metadata files (manifest and notes) with command: %s" % cmd2)
    execute(cmd2)

    log.info("Package %s has been correctly uploaded to %s:%s" % (package, host, path))

    return True
    
def distribute(folder, message,user, host, path, algorithm):
    return upload(package(folder, algorithm, message), user, host, path)
        


# TODO: write tests for this function
def process_command(options, args):
    """ I know how to take a list of program arguments and
    start doing the right thing with them"""
    cmd = args[0]
    cmd_args = args[1:]
    log.debug("processing '%s' command with args '%s'" % (cmd, '", "'.join(cmd_args)))
    log.debug("using options: %s" % options)

    if cmd == 'list':
        return list_manifest(options['manifest'])
    if cmd == 'validate':
        return validate_manifest(options['manifest'])
    elif cmd == 'add':
        return add_files(options['manifest'], options['algorithm'], cmd_args)
    elif cmd == 'purge':
        if options['cache_folder']:
            purge(folder=options['cache_folder'], gigs=options['size'])
        else:
            log.critical('please specify the cache folder to be purged')
            return False
    elif cmd == 'fetch':
        if not options.get('base_url'):
            log.critical('fetch command requires at least one url provided using ' +
                         'the url option in the command line')
            return False
        return fetch_files(options['manifest'], options['base_url'],
                           options['overwrite'], cmd_args,
                           cache_folder=options['cache_folder'])
    elif cmd == 'package':
        if not options.get('folder') or not options.get('message'):
            log.critical('package command requires a folder to be specified, containing the files to be added to the tooltool package, and a message providing info about the package')
            return False
        return package(options['folder'], options['algorithm'], options['message'])
    elif cmd == 'upload':
        if not options.get('package') or not options.get('user') or not options.get('host') or not options.get('path'):
             log.critical('upload command requires the package folder to be uploaded, and the user, host and path to be used to upload the tooltool upload server ')
             return False
        return upload(options.get('package'), options.get('user'), options.get('host'), options.get('path'))
    elif cmd == 'distribute':
        if not options.get('folder') or not options.get('message') or not options.get('user') or not options.get('host') or not options.get('path') :
             log.critical('distribute command requires the following parameters: --folder, --message, --user, --host, --path')
             return False
        return distribute(options.get('folder'), options.get('message'), options.get('user'), options.get('host'), options.get('path'), options.get('algorithm'))
    else:
        log.critical('command "%s" is not implemented' % cmd)
        return False


# fetching api:
#   http://hostname/algorithm/hash
#   example: http://people.mozilla.org/sha1/1234567890abcedf
# This will make it possible to have the server allow clients to
# use different algorithms than what was uploaded to the server

# TODO: Implement the following features:
#   -optimization: do small files first, justification is that they are faster
#    and cause a faster failure if they are invalid
#   -store permissions
#   -local renames i.e. call the file one thing on the server and
#    something different locally
#   -deal with the cases:
#     -local data matches file requested with different filename
#     -two different files with same name, different hash
#   -?only ever locally to digest as filename, symlink to real name
#   -?maybe deal with files as a dir of the filename with all files in that dir as the versions of that file
#      - e.g. ./python-2.6.7.dmg/0123456789abcdef and ./python-2.6.7.dmg/abcdef0123456789


def main():
    
    # Set up logging, for now just to the console
    ch = logging.StreamHandler()
    cf = logging.Formatter("%(levelname)s - %(message)s")
    ch.setFormatter(cf)

    # Set up option parsing
    parser = optparse.OptionParser()
    # I wish there was a way to say "only allow args to be
    # sequential and at the end of the argv.
    # OH! i could step through sys.argv and check for things starting without -/-- before things starting with them
    parser.add_option('-q', '--quiet', default=False,
                      dest='quiet', action='store_true')
    parser.add_option('-v', '--verbose', default=False,
                      dest='verbose', action='store_true')
    parser.add_option('-m', '--manifest', default=DEFAULT_MANIFEST_NAME,
                      dest='manifest', action='store',
                      help='specify the manifest file to be operated on')
    parser.add_option('-d', '--algorithm', default='sha512',
                      dest='algorithm', action='store',
                      help='openssl hashing algorithm to use')
    parser.add_option('-o', '--overwrite', default=False,
                      dest='overwrite', action='store_true',
                      help='if fetching, remote copy will overwrite a local copy that is different. ')
    parser.add_option('--url', dest='base_url', action='append',
                      help='base url for fetching files')
    parser.add_option('-c', '--cache-folder', dest='cache_folder',
                      help='Local cache folder')
    parser.add_option('-s', '--size',
                      help='free space required (in GB)', dest='size',
                      type='float', default=0.)

    parser.add_option('--folder',
                      help='the folder containing files to be added to a tooltool package ready to be uploaded to tooltool servers', dest='folder')
    parser.add_option('--message',
                      help='Any additional information about the tooltool package being generated and the files it includes', dest='message')

    parser.add_option('--package',
                      help='the folder containing files to be added to a tooltool package ready to be uploaded to tooltool servers', dest='package')
    parser.add_option('--user',
                      help='user to be used when uploading a tooltool package to a tooltool upload folder', dest='user')
    parser.add_option('--host',
                      help='host where to upload a tooltool package', dest='host')
    parser.add_option('--path',
                      help='Path on the tooltool upload server where to upload', dest='path')
    

    (options_obj, args) = parser.parse_args()
    # Dictionaries are easier to work with
    options = vars(options_obj)
    # Use some of the option parser to figure out application
    # log level
    if options.get('verbose'):
        ch.setLevel(logging.DEBUG)
    elif options.get('quiet'):
        ch.setLevel(logging.ERROR)
    else:
        ch.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    log.addHandler(ch)
   
    if not 'manifest' in options:
        parser.error("no manifest file specified")

    if len(args) < 1:
        parser.error('You must specify a command')
    exit(0 if process_command(options, args) else 1)

if __name__ == "__main__":
    main()
else:
    log.addHandler(logging.NullHandler())
    #log.addHandler(logging.StreamHandler())
