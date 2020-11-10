#!/usr/bin/env python3

#
# Copyright (c) YugaByte, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations
# under the License.
#


import argparse
import hashlib
import multiprocessing
import os
import platform
import random
import re
import subprocess
import sys
import time
from datetime import datetime

from build_definitions import *
import build_definitions
import_submodules(build_definitions)

CHECKSUM_FILE_NAME = 'thirdparty_src_checksums.txt'
CLOUDFRONT_URL = 'http://d3dr9sfxru4sde.cloudfront.net/{}'
MAX_FETCH_ATTEMPTS = 10
INITIAL_DOWNLOAD_RETRY_SLEEP_TIME_SEC = 1.0
DOWNLOAD_RETRY_SLEEP_INCREASE_SEC = 0.5


def hashsum_file(hash, filename, block_size=65536):
    with open(filename, "rb") as f:
        for block in iter(lambda: f.read(block_size), b""):
            hash.update(block)
    return hash.hexdigest()


def indent_lines(s, num_spaces=4):
    if s is None:
        return s
    return "\n".join([
        ' ' * num_spaces + line for line in s.split("\n")
    ])


def get_make_parallelism():
    return int(os.environ.get('YB_MAKE_PARALLELISM', multiprocessing.cpu_count()))


# This is the equivalent of shutil.which in Python 3.
def where_is_program(program_name):
    path = os.getenv('PATH')
    for path_dir in path.split(os.path.pathsep):
        full_path = os.path.join(path_dir, program_name)
        if os.path.exists(full_path) and os.access(full_path, os.X_OK):
            return full_path


g_is_ninja_available = None
def is_ninja_available():
    global g_is_ninja_available
    if g_is_ninja_available is None:
        g_is_ninja_available = bool(where_is_program('ninja'))
    return g_is_ninja_available


def compute_file_sha256(path):
    return hashsum_file(hashlib.sha256(), path)


class Builder:
    """
    This class manages the overall process of building third-party dependencies, including the set
    of dependencies to build, build types, and the directories to install dependencies.
    """
    def __init__(self):
        self.tp_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
        self.tp_build_dir = os.path.join(self.tp_dir, 'build')
        self.tp_src_dir = os.path.join(self.tp_dir, 'src')
        self.tp_download_dir = os.path.join(self.tp_dir, 'download')
        self.tp_installed_dir = os.path.join(self.tp_dir, 'installed')
        self.tp_installed_common_dir = os.path.join(self.tp_installed_dir, BUILD_TYPE_COMMON)
        self.tp_installed_llvm7_common_dir = os.path.join(
                self.tp_installed_dir + '_llvm7', BUILD_TYPE_COMMON)
        self.src_dir = os.path.dirname(self.tp_dir)
        if not os.path.isdir(self.src_dir):
            fatal('YB src directory "{}" does not exist'.format(self.src_dir))
        self.build_support_dir = os.path.join(self.src_dir, 'build-support')
        self.enterprise_root = os.path.join(self.src_dir, 'ent')

        self.dependencies = [
            build_definitions.zlib.ZLibDependency(),
            build_definitions.lz4.LZ4Dependency(),
            build_definitions.openssl.OpenSSLDependency(),
            build_definitions.libev.LibEvDependency(),
            build_definitions.rapidjson.RapidJsonDependency(),
            build_definitions.squeasel.SqueaselDependency(),
            build_definitions.curl.CurlDependency(),
            build_definitions.hiredis.HiRedisDependency(),
            build_definitions.cqlsh.CQLShDependency(),
            build_definitions.redis_cli.RedisCliDependency(),
            build_definitions.flex.FlexDependency(),
            build_definitions.bison.BisonDependency(),
            build_definitions.icu4c.Icu4cDependency(),
            build_definitions.libedit.LibEditDependency(),
            build_definitions.openldap.OpenLDAPDependency(),
        ]

        if is_linux():
            self.dependencies += [
                build_definitions.libuuid.LibUuidDependency(),
                build_definitions.llvm.LLVMDependency(),
                build_definitions.libcxx.LibCXXDependency(),

                build_definitions.libunwind.LibUnwindDependency(),
                build_definitions.libbacktrace.LibBacktraceDependency(),
                build_definitions.include_what_you_use.IncludeWhatYouUseDependency(),
            ]

        self.dependencies += [
            build_definitions.protobuf.ProtobufDependency(),
            build_definitions.crypt_blowfish.CryptBlowfishDependency(),
            build_definitions.boost.BoostDependency(),

            build_definitions.gflags.GFlagsDependency(),
            build_definitions.glog.GLogDependency(),
            build_definitions.gperftools.GPerfToolsDependency(),
            build_definitions.gmock.GMockDependency(),
            build_definitions.snappy.SnappyDependency(),
            build_definitions.crcutil.CRCUtilDependency(),
            build_definitions.libcds.LibCDSDependency(),
            build_definitions.abseil.AbseilDependency(),
            build_definitions.tcmalloc.TCMallocDependency(),

            build_definitions.libuv.LibUvDependency(),
            build_definitions.cassandra_cpp_driver.CassandraCppDriverDependency(),
        ]

        self.selected_dependencies = []

        self.using_linuxbrew = False
        self.linuxbrew_dir = None
        self.cc = None
        self.cxx = None
        self.args = None

        self.detect_linuxbrew()
        self.load_expected_checksums()

    def set_compiler(self, compiler_type):
        if is_mac():
            if compiler_type != 'clang':
                raise ValueError(
                    "Cannot set compiler type to %s on macOS, only clang is supported" %
                        compiler_type)
            self.compiler_type = 'clang'
        else:
            self.compiler_type = compiler_type

        os.environ['YB_COMPILER_TYPE'] = compiler_type
        self.find_compiler_by_type(compiler_type)

        c_compiler = self.get_c_compiler()
        cxx_compiler = self.get_cxx_compiler()

        os.environ['CC'] = c_compiler
        os.environ['CXX'] = cxx_compiler

    def init(self):
        os.environ['YB_IS_THIRDPARTY_BUILD'] = '1'

        parser = argparse.ArgumentParser(prog=sys.argv[0])
        parser.add_argument('--build-type',
                            default=None,
                            type=str,
                            choices=BUILD_TYPES,
                            help='Build only specific part of thirdparty dependencies.')
        parser.add_argument('--skip-sanitizers',
                            action='store_true',
                            help='Do not build ASAN and TSAN instrumented dependencies.')
        parser.add_argument('--clean',
                            action='store_const',
                            const=True,
                            default=False,
                            help='Clean.')
        parser.add_argument('--add_checksum',
                            help='Compute and add unknown checksums to %s' % CHECKSUM_FILE_NAME,
                            action='store_true')
        parser.add_argument('--skip',
                            help='Dependencies to skip')
        parser.add_argument('dependencies',
            nargs=argparse.REMAINDER, help='Dependencies to build.')
        parser.add_argument('-j', '--make-parallelism',
                            help='How many cores should the build use. This is passed to '
                                 'Make/Ninja child processes. This can also be specified using the '
                                 'YB_MAKE_PARALLELISM environment variable.',
                            type=int)
        self.args = parser.parse_args()

        if self.args.dependencies and self.args.skip:
            raise ValueError(
                "--skip is not compatible with specifying a list of dependencies to build")

        if self.args.dependencies:
            names = set([dep.name for dep in self.dependencies])
            for dep in self.args.dependencies:
                if dep not in names:
                    fatal("Unknown dependency name: %s", dep)
            for dep in self.dependencies:
                if dep.name in self.args.dependencies:
                    self.selected_dependencies.append(dep)
        elif self.args.skip:
            skipped = set(self.args.skip.split(','))
            log("Skipping dependencies: %s", sorted(skipped))
            self.selected_dependencies = []
            for dependency in self.dependencies:
                if dependency.name in skipped:
                    skipped.remove(dependency.name)
                else:
                    self.selected_dependencies.append(dependency)
            if skipped:
                raise ValueError("Unknown dependencies, cannot skip: %s" % sorted(skipped))
        else:
            self.selected_dependencies = self.dependencies

        if self.args.make_parallelism:
            os.environ['YB_MAKE_PARALLELISM'] = str(self.args.make_parallelism)

    def run(self):
        self.set_compiler('clang' if is_mac() else 'gcc')
        if self.args.clean:
            self.clean()
        self.prepare_out_dirs()
        self.curl_path = which('curl')
        os.environ['PATH'] = ':'.join([
                os.path.join(self.tp_installed_common_dir, 'bin'),
                os.path.join(self.tp_installed_llvm7_common_dir, 'bin'),
                os.environ['PATH']
        ])
        self.build(BUILD_TYPE_COMMON)
        if is_linux():
            self.build(BUILD_TYPE_UNINSTRUMENTED)
            # GCC8 has been temporarily removed, since it relies on broken Linuxbrew distribution.
            # See https://github.com/yugabyte/yugabyte-db/issues/3044#issuecomment-560639105
            # self.build(BUILD_TYPE_GCC8_UNINSTRUMENTED)
        self.build(BUILD_TYPE_CLANG_UNINSTRUMENTED)
        if is_linux() and not self.args.skip_sanitizers:
            self.build(BUILD_TYPE_ASAN)
            self.build(BUILD_TYPE_TSAN)

    def find_compiler_by_type(self, compiler_type):
        compilers = None
        if compiler_type == 'gcc':
            compilers = self.find_gcc()
        elif compiler_type == 'gcc8':
            compilers = self.find_gcc8()
        elif compiler_type == 'clang':
            compilers = self.find_clang()
        else:
            fatal("Unknown compiler type {}".format(compiler_type))

        for compiler in compilers:
            if compiler is None or not os.path.exists(compiler):
                fatal("Compiler executable does not exist: {}".format(compiler))

        self.cc = compilers[0]
        self.cxx = compilers[1]

    def get_c_compiler(self):
        assert self.cc is not None
        return self.cc

    def get_cxx_compiler(self):
        assert self.cxx is not None
        return self.cxx

    def find_gcc(self):
        return self.do_find_gcc('gcc', 'g++')

    def find_gcc8(self):
        return self.do_find_gcc('gcc-8', 'g++-8')

    def do_find_gcc(self, c_compiler, cxx_compiler):
        if 'YB_GCC_PREFIX' in os.environ:
            gcc_dir = os.environ['YB_GCC_PREFIX']
        elif self.using_linuxbrew:
            gcc_dir = self.linuxbrew_dir
        else:
            return which(c_compiler), which(cxx_compiler)

        gcc_bin_dir = os.path.join(gcc_dir, 'bin')

        if not os.path.isdir(gcc_bin_dir):
            fatal("Directory {} does not exist".format(gcc_bin_dir))

        return os.path.join(gcc_bin_dir, 'gcc'), os.path.join(gcc_bin_dir, 'g++')

    def find_clang(self):
        clang_dir = None
        if 'YB_CLANG_PREFIX' in os.environ:
            clang_dir = os.environ['YB_CLANG_PREFIX']
        else:
            candidate_dirs = [
                os.path.join(self.tp_dir, 'clang-toolchain'),
                '/usr'
            ]
            for dir in candidate_dirs:
                bin_dir = os.path.join(dir, 'bin')
                if os.path.isdir(bin_dir) and os.path.exists(os.path.join(bin_dir, 'clang')):
                    clang_dir = dir
                    break
            if clang_dir is None:
                fatal("Failed to find clang at the following locations: {}".format(candidate_dirs))

        clang_bin_dir = os.path.join(clang_dir, 'bin')

        return os.path.join(clang_bin_dir, 'clang'), os.path.join(clang_bin_dir, 'clang++')

    def detect_linuxbrew(self):
        if not is_linux():
            return

        self.linuxbrew_dir = os.getenv('YB_LINUXBREW_DIR')

        if self.linuxbrew_dir:
            self.using_linuxbrew = True
            os.environ['PATH'] = os.path.join(self.linuxbrew_dir, 'bin') + ':' + os.environ['PATH']

    def clean(self):
        heading('Clean')
        for dependency in self.selected_dependencies:
            for dir_name in BUILD_TYPES:
                for leaf in [dependency.name, '.build-stamp-{}'.format(dependency)]:
                    path = os.path.join(self.tp_build_dir, dir_name, leaf)
                    if os.path.exists(path):
                        log("Removing %s build output: %s", dependency.name, path)
                        remove_path(path)
            if dependency.dir_name is not None:
                src_dir = self.source_path(dependency)
                if os.path.exists(src_dir):
                    log("Removing %s source: %s", dependency.name, src_dir)
                    remove_path(src_dir)

            archive_path = self.archive_path(dependency)
            if archive_path is not None:
                log("Removing %s archive: %s", dependency.name, archive_path)
                remove_path(archive_path)

    def download_dependency(self, dep):
        src_path = self.source_path(dep)
        patch_level_path = os.path.join(src_path, 'patchlevel-{}'.format(dep.patch_version))
        if os.path.exists(patch_level_path):
            return

        download_url = dep.download_url
        if download_url is None:
            download_url = CLOUDFRONT_URL.format(dep.archive_name)
            log("Using legacy download URL: %s (we should consider moving this to GitHub)",
                download_url)

        archive_path = self.archive_path(dep)

        remove_path(src_path)
        # If download_url is "mkdir" then we just create empty directory with specified name.
        if download_url != 'mkdir':
            if archive_path is None:
                return
            self.ensure_file_downloaded(download_url, archive_path)
            self.extract_archive(archive_path,
                                 os.path.dirname(src_path),
                                 os.path.basename(src_path))
        else:
            log("Creating %s", src_path)
            mkdir_if_missing(src_path)

        if hasattr(dep, 'extra_downloads'):
            for extra in dep.extra_downloads:
                archive_path = os.path.join(self.tp_download_dir, extra.archive_name)
                log("Downloading %s from %s", extra.archive_name, extra.download_url)
                self.ensure_file_downloaded(extra.download_url, archive_path)
                output_path = os.path.join(src_path, extra.dir_name)
                self.extract_archive(archive_path, output_path)
                if hasattr(extra, 'post_exec'):
                    with PushDir(output_path):
                        if isinstance(extra.post_exec[0], str):
                            subprocess.check_call(extra.post_exec)
                        else:
                            for command in extra.post_exec:
                                subprocess.check_call(command)

        if hasattr(dep, 'patches'):
            with PushDir(src_path):
                for patch in dep.patches:
                    log("Applying patch: %s", patch)
                    process = subprocess.Popen(['patch', '-p{}'.format(dep.patch_strip)],
                                               stdin=subprocess.PIPE)
                    with open(os.path.join(self.tp_dir, 'patches', patch), 'rt') as inp:
                        patch = inp.read()
                    process.stdin.write(patch.encode('utf-8'))
                    process.stdin.close()
                    exit_code = process.wait()
                    if exit_code:
                        fatal("Patch {} failed with code: {}".format(dep.name, exit_code))
                if hasattr(dep, 'post_patch'):
                    subprocess.check_call(dep.post_patch)

        with open(patch_level_path, 'wb') as out:
            # Just create an empty file.
            pass

    def archive_path(self, dep):
        if dep.archive_name is None:
            return None
        return os.path.join(self.tp_download_dir, dep.archive_name)


    def source_path(self, dep):
        return os.path.join(self.tp_src_dir, dep.dir_name)

    def get_checksum_file(self):
        return os.path.join(self.tp_dir, CHECKSUM_FILE_NAME)

    def load_expected_checksums(self):
        checksum_file = self.get_checksum_file()
        if not os.path.exists(checksum_file):
            fatal("Expected checksum file not found at {}".format(checksum_file))

        self.filename2checksum = {}
        with open(checksum_file, 'rt') as inp:
            for line in inp:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                sum, fname = line.split(None, 1)
                if not re.match('^[0-9a-f]{64}$', sum):
                    fatal("Invalid checksum: '{}' for archive name: '{}' in {}. Expected to be a "
                                  "SHA-256 sum (64 hex characters)."
                                  .format(sum, fname, checksum_file))
                self.filename2checksum[fname] = sum

    def get_expected_checksum(self, filename, downloaded_path):
        if filename not in self.filename2checksum:
            if self.args.add_checksum:
                checksum_file = self.get_checksum_file()
                with open(checksum_file, 'rt') as inp:
                    lines = inp.readlines()
                lines = [line.rstrip() for line in lines]
                checksum = compute_file_sha256(downloaded_path)
                lines.append("%s  %s" % (checksum, filename))
                with open(checksum_file, 'wt') as out:
                    for line in lines:
                        out.write(line + "\n")
                self.filename2checksum[filename] = checksum
                log("Added checksum for %s to %s: %s", filename, checksum_file, checksum)
                return checksum

            fatal("No expected checksum provided for {}".format(filename))
        return self.filename2checksum[filename]

    def ensure_file_downloaded(self, url, path):
        filename = os.path.basename(path)

        mkdir_if_missing(self.tp_download_dir)

        if os.path.exists(path):
            # We check the filename against our checksum map only if the file exists. This is done
            # so that we would still download the file even if we don't know the checksum, making it
            # easier to add new third-party dependencies.
            expected_checksum = self.get_expected_checksum(filename, downloaded_path=path)
            if self.verify_checksum(path, expected_checksum):
                log("No need to re-download %s: checksum already correct", filename)
                return
            log("File %s already exists but has wrong checksum, removing", path)
            remove_path(path)

        log("Fetching %s", filename)
        sleep_time_sec = INITIAL_DOWNLOAD_RETRY_SLEEP_TIME_SEC
        for attempt_index in range(1, MAX_FETCH_ATTEMPTS + 1):
            try:
                subprocess.check_call([self.curl_path, '-o', path, '--location', url])
                break
            except subprocess.CalledProcessError as ex:
                log("Error downloading %s (attempt %d): %s",
                    self.curl_path, attempt_index, str(ex))
                if attempt_index == MAX_FETCH_ATTEMPTS:
                    log("Giving up after %d attempts", MAX_FETCH_ATTEMPTS)
                    raise ex
                log("Will retry after %.1f seconds", sleep_time_sec)
                time.sleep(sleep_time_sec)
                sleep_time_sec += DOWNLOAD_RETRY_SLEEP_INCREASE_SEC

        if not os.path.exists(path):
            fatal("Downloaded '%s' but but unable to find '%s'", url, path)
        expected_checksum = self.get_expected_checksum(filename, downloaded_path=path)
        if not self.verify_checksum(path, expected_checksum):
            fatal("File '%s' has wrong checksum after downloading from '%s'. "
                  "Has %s, but expected: %s",
                  path, url, compute_file_sha256(path), expected_checksum)

    def verify_checksum(self, filename, expected_checksum):
        real_checksum = hashsum_file(hashlib.sha256(), filename)
        return real_checksum == expected_checksum

    def extract_archive(self, archive_filename, out_dir, out_name=None):
        """
        Extract the given archive into a subdirectory of out_dir, optionally renaming it to
        the specified name out_name. The archive is expected to contain exactly one directory.
        If out_name is not specified, the name of the directory inside the archive becomes
        the name of the destination directory.

        out_dir is the parent directory that should contain the extracted directory when the
        function returns.
        """

        def dest_dir_already_exists(full_out_path):
            if os.path.exists(full_out_path):
                log("Directory already exists: %s, skipping extracting %s" % (
                        full_out_path, archive_filename))
                return True
            return False

        full_out_path = None
        if out_name:
            full_out_path = os.path.join(out_dir, out_name)
            if dest_dir_already_exists(full_out_path):
                return

        # Extract the archive into a temporary directory.
        tmp_out_dir = os.path.join(
            out_dir, 'tmp-extract-%s-%s-%d' % (
                os.path.basename(archive_filename),
                datetime.now().strftime('%Y-%m-%dT%H_%M_%S'),  # Current second-level timestamp.
                random.randint(10 ** 8, 10 ** 9 - 1)))  # A random 9-digit integer.
        if os.path.exists(tmp_out_dir):
            raise IOError("Just-generated unique directory name already exists: %s" % tmp_out_dir)
        os.makedirs(tmp_out_dir)

        archive_extension = None
        for ext in ARCHIVE_TYPES:
            if archive_filename.endswith(ext):
                archive_extension = ext
                break
        if not archive_extension:
            fatal("Unknown archive type for: {}".format(archive_filename))

        try:
            with PushDir(tmp_out_dir):
                cmd = ARCHIVE_TYPES[archive_extension].format(archive_filename)
                log("Extracting %s in temporary directory %s", cmd, tmp_out_dir)
                subprocess.check_call(cmd, shell=True)
                extracted_subdirs = [
                    subdir_name for subdir_name in os.listdir(tmp_out_dir)
                    if not subdir_name.startswith('.')
                ]
                if len(extracted_subdirs) != 1:
                    raise IOError(
                        "Expected the extracted archive %s to contain exactly one "
                        "subdirectory and no files, found: %s" % (
                            archive_filename, extracted_subdirs))
                extracted_subdir_basename = extracted_subdirs[0]
                extracted_subdir_path = os.path.join(tmp_out_dir, extracted_subdir_basename)
                if not os.path.isdir(extracted_subdir_path):
                    raise IOError(
                        "This is a file, expected it to be a directory: %s" %
                        extracted_subdir_path)

                if not full_out_path:
                    full_out_path = os.path.join(out_dir, extracted_subdir_basename)
                    if dest_dir_already_exists(full_out_path):
                        return

                log("Moving %s to %s", extracted_subdir_path, full_out_path)
                shutil.move(extracted_subdir_path, full_out_path)
        finally:
            log("Removing temporary directory: %s", tmp_out_dir)
            shutil.rmtree(tmp_out_dir)

    def prepare_out_dirs(self):
        dirs = [os.path.join(self.tp_installed_dir, type) for type in BUILD_TYPES]
        libcxx_dirs = [os.path.join(dir, 'libcxx') for dir in dirs]
        for dir in dirs + libcxx_dirs:
            lib_dir = os.path.join(dir, 'lib')
            mkdir_if_missing(lib_dir)
            mkdir_if_missing(os.path.join(dir, 'include'))
            # On some systems, autotools installs libraries to lib64 rather than lib.    Fix
            # this by setting up lib64 as a symlink to lib.    We have to do this step first
            # to handle cases where one third-party library depends on another.    Make sure
            # we create a relative symlink so that the entire PREFIX_DIR could be moved,
            # e.g. after it is packaged and then downloaded on a different build node.
            lib64_dir = os.path.join(dir, 'lib64')
            if os.path.exists(lib64_dir):
                if os.path.islink(lib64_dir):
                    continue
                remove_path(lib64_dir)
            os.symlink('lib', lib64_dir)

    def init_flags(self):
        self.preprocessor_flags = []
        self.ld_flags = []
        self.compiler_flags = []
        self.c_flags = []
        self.cxx_flags = []
        self.libs = []

        self.add_linuxbrew_flags()
        # -fPIC is there to always generate position-independent code, even for static libraries.
        self.preprocessor_flags.append(
            '-I{}'.format(os.path.join(self.tp_installed_common_dir, 'include')))
        self.compiler_flags += self.preprocessor_flags
        self.compiler_flags += ['-fno-omit-frame-pointer', '-fPIC', '-O2', '-Wall']
        self.ld_flags.append('-L{}'.format(os.path.join(self.tp_installed_common_dir, 'lib')))
        if is_linux():
            # On Linux, ensure we set a long enough rpath so we can change it later with chrpath or
            # a similar tool.
            self.add_rpath(
                    "/tmp/making_sure_we_have_enough_room_to_set_rpath_later_{}_end_of_rpath"
                    .format('_' * 256))

            self.dylib_suffix = "so"
        elif is_mac():
            self.dylib_suffix = "dylib"

            # YugaByte builds with C++11, which on OS X requires using libc++ as the standard
            # library implementation. Some of the dependencies do not compile against libc++ by
            # default, so we specify it explicitly.
            self.cxx_flags.append("-stdlib=libc++")
            self.libs += ["-lc++", "-lc++abi"]
            # Build for macOS Mojave or later. See https://bit.ly/37myHbk
            self.compiler_flags.append("-mmacosx-version-min=10.14")
        else:
            fatal("Unsupported platform: {}".format(platform.system()))
        # The C++ standard must match CMAKE_CXX_STANDARD our top-level CMakeLists.txt.
        self.cxx_flags.append('-std=c++14')

    def add_linuxbrew_flags(self):
        if self.using_linuxbrew:
            lib_dir = os.path.join(self.linuxbrew_dir, 'lib')
            self.ld_flags.append(" -Wl,-dynamic-linker={}".format(os.path.join(lib_dir, 'ld.so')))
            self.add_lib_dir_and_rpath(lib_dir)

    def add_lib_dir_and_rpath(self, lib_dir):
        self.ld_flags.append("-L{}".format(lib_dir))
        self.add_rpath(lib_dir)

    def prepend_lib_dir_and_rpath(self, lib_dir):
        self.ld_flags.insert(0, "-L{}".format(lib_dir))
        self.prepend_rpath(lib_dir)

    def add_rpath(self, path):
        self.ld_flags.append("-Wl,-rpath,{}".format(path))

    def prepend_rpath(self, path):
        self.ld_flags.insert(0, "-Wl,-rpath,{}".format(path))

    def log_prefix(self, dep):
        return '{} ({})'.format(dep.name, self.build_type)

    def build_with_configure(
            self,
            log_prefix,
            extra_args=None,
            jobs=None,
            configure_cmd=['./configure'],
            install=['install'],
            autoconf=False,
            source_subdir=None):
        os.environ["YB_REMOTE_COMPILATION"] = "0"
        dir_for_build = os.getcwd()
        if source_subdir:
            dir_for_build = os.path.join(dir_for_build, source_subdir)

        with PushDir(dir_for_build):
            log("Building in %s", dir_for_build)
            if autoconf:
                log_output(log_prefix, ['autoreconf', '-i'])

            configure_args = configure_cmd.copy() + ['--prefix={}'.format(self.prefix)]
            if extra_args is not None:
                configure_args += extra_args
            log_output(log_prefix, configure_args)

            if not jobs:
                jobs = get_make_parallelism()
            log_output(log_prefix, ['make', '-j{}'.format(jobs)])
            if install:
                log_output(log_prefix, ['make'] + install)

    def build_with_cmake(self, dep, extra_args=None, use_ninja=False, **kwargs):
        if use_ninja == 'auto':
            use_ninja = is_ninja_available()
            log('Ninja is %s', 'available' if use_ninja else 'unavailable')

        log("Building dependency %s using CMake with arguments: %s, use_ninja=%s",
            dep, extra_args, use_ninja)
        log_prefix = self.log_prefix(dep)
        os.environ["YB_REMOTE_COMPILATION"] = "0"

        remove_path('CMakeCache.txt')
        remove_path('CMakeFiles')

        src_dir = self.source_path(dep)
        if 'src_dir' in kwargs:
            src_dir = os.path.join(src_dir, kwargs['src_dir'])
        args = ['cmake', src_dir]
        if use_ninja:
            args += ['-G', 'Ninja']
        if extra_args is not None:
            args += extra_args

        log_output(log_prefix, args)

        build_tool = 'ninja' if use_ninja else 'make'
        build_tool_cmd = [build_tool, '-j{}'.format(get_make_parallelism())]

        log_output(log_prefix, build_tool_cmd)

        if 'install' not in kwargs or kwargs['install']:
            log_output(log_prefix, [build_tool, 'install'])

    def build(self, build_type):
        if build_type != BUILD_TYPE_COMMON and self.args.build_type is not None:
            if build_type != self.args.build_type:
                return

        self.set_build_type(build_type)
        self.setup_compiler()
        # This is needed at least for glog to be able to find gflags.
        self.add_rpath(os.path.join(self.tp_installed_dir, self.build_type, 'lib'))
        build_group = (
            BUILD_GROUP_COMMON if build_type == BUILD_TYPE_COMMON
                               else BUILD_GROUP_INSTRUMENTED
        )

        for dep in self.selected_dependencies:
            if dep.build_group == build_group and dep.should_build(self):
                self.build_dependency(dep)

    def get_prefix(self, qualifier=None):
        return os.path.join(
            self.tp_installed_dir + ('_' + qualifier if qualifier else ''),
            self.build_type)

    def set_build_type(self, build_type):
        self.build_type = build_type
        self.find_prefix = self.tp_installed_common_dir
        self.prefix = self.get_prefix()
        if build_type != BUILD_TYPE_COMMON:
            self.find_prefix += ';' + self.prefix
        self.prefix_bin = os.path.join(self.prefix, 'bin')
        self.prefix_lib = os.path.join(self.prefix, 'lib')
        self.prefix_include = os.path.join(self.prefix, 'include')
        if self.building_with_clang():
            compiler = 'clang'
        elif build_type == BUILD_TYPE_GCC8_UNINSTRUMENTED:
            compiler = 'gcc8'
        else:
            compiler = 'gcc'
        self.set_compiler(compiler)
        heading("Building {} dependencies (compiler type: {})".format(
            build_type, self.compiler_type))
        log("Compiler type: %s", self.compiler_type)
        log("C compiler: %s", self.get_c_compiler())
        log("C++ compiler: %s", self.get_cxx_compiler())

    def setup_compiler(self):
        self.init_flags()
        if is_mac() or not self.building_with_clang():
            return
        if self.build_type == BUILD_TYPE_ASAN:
            self.compiler_flags += ['-fsanitize=address', '-fsanitize=undefined',
                                    '-DADDRESS_SANITIZER']
        elif self.build_type == BUILD_TYPE_TSAN:
            self.compiler_flags += ['-fsanitize=thread', '-DTHREAD_SANITIZER']
        elif self.build_type == BUILD_TYPE_CLANG_UNINSTRUMENTED:
            pass
        else:
            fatal("Wrong instrumentation type: {}".format(self.build_type))
        stdlib_suffix = self.build_type
        stdlib_path = os.path.join(self.tp_installed_dir, stdlib_suffix, 'libcxx')
        stdlib_include = os.path.join(stdlib_path, 'include', 'c++', 'v1')
        stdlib_lib = os.path.join(stdlib_path, 'lib')
        self.cxx_flags.insert(0, '-nostdinc++')
        self.cxx_flags.insert(0, '-isystem')
        self.cxx_flags.insert(1, stdlib_include)
        self.cxx_flags.insert(0, '-stdlib=libc++')
        # Clang complains about argument unused during compilation: '-stdlib=libc++' when both
        # -stdlib=libc++ and -nostdinc++ are specified.
        self.cxx_flags.insert(0, '-Wno-error=unused-command-line-argument')
        self.prepend_lib_dir_and_rpath(stdlib_lib)
        if self.using_linuxbrew:
            self.compiler_flags.append('--gcc-toolchain={}'.format(self.linuxbrew_dir))

    def build_dependency(self, dep):
        if not self.should_rebuild_dependency(dep):
            return
        log("")
        colored_log(YELLOW_COLOR, SEPARATOR)
        colored_log(YELLOW_COLOR, "Building %s (%s)", dep.name, self.build_type)
        colored_log(YELLOW_COLOR, SEPARATOR)

        self.download_dependency(dep)

        # Additional flags coming from the dependency itself.
        dep_additional_cxx_flags = (dep.get_additional_cxx_flags(self) +
                                    dep.get_additional_c_cxx_flags(self))
        dep_additional_c_flags = (dep.get_additional_c_flags(self) +
                                  dep.get_additional_c_cxx_flags(self))

        os.environ["CXXFLAGS"] = " ".join(
                self.compiler_flags + self.cxx_flags + dep_additional_cxx_flags)
        os.environ["CFLAGS"] = " ".join(
                self.compiler_flags + self.c_flags + dep_additional_c_flags)
        os.environ["LDFLAGS"] = " ".join(self.ld_flags)
        os.environ["LIBS"] = " ".join(self.libs)
        os.environ["CPPFLAGS"] = " ".join(self.preprocessor_flags)

        with PushDir(self.create_build_dir_and_prepare(dep)):
            dep.build(self)
        self.save_build_stamp_for_dependency(dep)
        log("")
        log("Finished building %s (%s)", dep.name, self.build_type)
        log("")

    # Determines if we should rebuild a component with the given name based on the existing "stamp"
    # file and the current value of the "stamp" (based on Git SHA1 and local changes) for the
    # component. The result is returned in should_rebuild_component_rv variable, which should have
    # been made local by the caller.
    def should_rebuild_dependency(self, dep):
        stamp_path = self.get_build_stamp_path_for_dependency(dep)
        old_build_stamp = None
        if os.path.exists(stamp_path):
            with open(stamp_path, 'rt') as inp:
                old_build_stamp = inp.read()

        new_build_stamp = self.get_build_stamp_for_dependency(dep)

        if dep.dir_name is not None:
            src_dir = self.source_path(dep)
            if not os.path.exists(src_dir):
                log("Have to rebuild %s (%s): source dir %s does not exist",
                    dep.name, self.build_type, src_dir)
                return True

        if old_build_stamp == new_build_stamp:
            log("Not rebuilding %s (%s) -- nothing changed.", dep.name, self.build_type)
            return False
        else:
            log("Have to rebuild %s (%s):", dep.name, self.build_type)
            log("Old build stamp for %s (from %s):\n%s",
                dep.name, stamp_path, indent_lines(old_build_stamp))
            log("New build stamp for %s:\n%s",
                dep.name, indent_lines(new_build_stamp))
            return True

    def get_build_stamp_path_for_dependency(self, dep):
        return os.path.join(self.tp_build_dir, self.build_type, '.build-stamp-{}'.format(dep.name))

    # Come up with a string that allows us to tell when to rebuild a particular third-party
    # dependency. The result is returned in the get_build_stamp_for_component_rv variable, which
    # should have been made local by the caller.
    def get_build_stamp_for_dependency(self, dep):
        input_files_for_stamp = ['yb_build_thirdparty_main.py',
                                 'build_thirdparty.sh',
                                 os.path.join('build_definitions',
                                              '{}.py'.format(dep.name.replace('-', '_')))]

        for path in input_files_for_stamp:
            abs_path = os.path.join(self.tp_dir, path)
            if not os.path.exists(abs_path):
                fatal("File '{}' does not exist -- expecting it to exist when creating a 'stamp' " \
                            "for the build configuration of '{}'.".format(abs_path, dep.name))

        with PushDir(self.tp_dir):
            git_commit_sha1 = subprocess.check_output(
                    ['git', 'log', '--pretty=%H', '-n', '1'] + input_files_for_stamp).strip()
            build_stamp = 'git_commit_sha1={}\n'.format(git_commit_sha1)
            for git_extra_args in ([], ['--cached']):
                git_diff = subprocess.check_output(
                    ['git', 'diff'] + git_extra_args + input_files_for_stamp)
                git_diff_sha256 = hashlib.sha256(git_diff).hexdigest()
                build_stamp += 'git_diff_sha256{}={}\n'.format(
                    '_'.join(git_extra_args).replace('--', '_'),
                    git_diff_sha256)
            return build_stamp

    def save_build_stamp_for_dependency(self, dep):
        stamp = self.get_build_stamp_for_dependency(dep)
        stamp_path = self.get_build_stamp_path_for_dependency(dep)

        log("Saving new build stamp to '%s':\n%s", stamp_path, indent_lines(stamp))
        with open(stamp_path, "wt") as out:
            out.write(stamp)

    def create_build_dir_and_prepare(self, dep):
        src_dir = self.source_path(dep)
        if not os.path.isdir(src_dir):
            fatal("Directory '{}' does not exist".format(src_dir))

        build_dir = os.path.join(self.tp_build_dir, self.build_type, dep.dir_name)
        mkdir_if_missing(build_dir)

        if dep.copy_sources:
            log("Bootstrapping %s from %s", build_dir, src_dir)
            subprocess.check_call(['rsync', '-a', src_dir + '/', build_dir])
        return build_dir

    def is_release_build(self):
        return self.build_type == BUILD_TYPE_GCC8_UNINSTRUMENTED or \
               self.build_type == BUILD_TYPE_UNINSTRUMENTED or \
               self.build_type == BUILD_TYPE_CLANG_UNINSTRUMENTED

    def cmake_build_type(self):
        return 'Release' if self.is_release_build() else 'Debug'

    # Returns true if we are using clang to build current build_type.
    def building_with_clang(self):
        if is_mac():
            # We only support clang on macOS.
            return True
        return self.build_type == BUILD_TYPE_ASAN or self.build_type == BUILD_TYPE_TSAN or \
               self.build_type == BUILD_TYPE_CLANG_UNINSTRUMENTED

    # Returns true if we will need clang to complete full thirdparty build, requested by user.
    def will_need_clang(self):
        return self.args.build_type != BUILD_TYPE_UNINSTRUMENTED and \
               self.args.build_type != BUILD_TYPE_GCC8_UNINSTRUMENTED

    def check_cxx_compiler_flag(self, flag):
        process = subprocess.Popen([self.get_cxx_compiler(), '-x', 'c++', flag, '-'],
                                   stdin=subprocess.PIPE)
        process.stdin.write("int main() { return 0; }".encode('utf-8'))
        process.stdin.close()
        return process.wait() == 0

    def add_checked_flag(self, flags, flag):
        if self.check_cxx_compiler_flag(flag):
            flags.append(flag)

    def get_openssl_dir(self):
        return os.path.join(self.tp_installed_common_dir)

    def get_openssl_related_cmake_args(self):
        """
        Returns a list of CMake arguments to use to pick up the version of OpenSSL that we should be
        using. Returns an empty list if the default OpenSSL installation should be used.
        """
        openssl_dir = self.get_openssl_dir()
        openssl_options = ['-DOPENSSL_ROOT_DIR=' + openssl_dir]
        openssl_crypto_library = os.path.join(openssl_dir, 'lib', 'libcrypto.' + self.dylib_suffix)
        openssl_ssl_library = os.path.join(openssl_dir, 'lib', 'libssl.' + self.dylib_suffix)
        openssl_options += [
            '-DOPENSSL_CRYPTO_LIBRARY=' + openssl_crypto_library,
            '-DOPENSSL_SSL_LIBRARY=' + openssl_ssl_library,
            '-DOPENSSL_LIBRARIES=%s;%s' % (openssl_crypto_library, openssl_ssl_library)
        ]
        return openssl_options

class LibTestBase:
    """
    Verify correct library paths are used in installed dynamically-linked executables and
    libraries.
    """
    def __init__(self):
        self.tp_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
        self.tp_installed_dir = os.path.join(self.tp_dir, 'installed')

    def init_regex(self):
        self.okay_paths = re.compile("|".join(self.lib_re_list))

    def check_lib_deps(self, file_path, cmdout):
        status = True
        for line in cmdout.splitlines():
            if not self.okay_paths.match(line):
                if status:
                    log(file_path + ":")
                log("Bad path: %s", line)
                status = False
        return status

    # overridden in platform specific classes
    def good_libs(self, file_path):
        pass

    def run(self):
        self.init_regex()
        heading("Scanning installed executables and libraries...")
        test_pass = True
        # files to examine are much reduced if we look only at bin and lib directories
        dir_pattern = re.compile('^(lib|libcxx|[s]bin)$')
        dirs = [os.path.join(self.tp_installed_dir, type) for type in BUILD_TYPES]
        for installed_dir in dirs:
            with os.scandir(installed_dir) as candidate_dirs:
                for candidate in candidate_dirs:
                    if dir_pattern.match(candidate.name):
                        examine_path = os.path.join(installed_dir, candidate.name)
                        for dirpath, dirnames, files in os.walk(examine_path):
                            for file_name in files:
                                full_path = os.path.join(dirpath, file_name)
                                if os.path.islink(full_path):
                                    continue
                                if not self.good_libs(full_path):
                                    test_pass = False
        if not test_pass:
            fatal(f"Found problematic library dependencies, using tool: {self.tool}")
        else:
            log("No problems found with library dependencies.")


class LibTestMac(LibTestBase):
    def __init__(self):
        super().__init__()
        self.tool = "otool -L"
        self.lib_re_list = ["^\t/usr/",
                            "^\t/System/Library/",
                            "^Archive ",
                            "^/",
                            "^\t@rpath",
                            "^\t@loader_path",
                            f"^\t{self.tp_dir}"]

    def good_libs(self, file_path):
        libout = subprocess.check_output(['otool', '-L', file_path]).decode('utf-8')
        if 'is not an object file' in libout:
            return True
        return self.check_lib_deps(file_path, libout)


class LibTestLinux(LibTestBase):
    def __init__(self):
        super().__init__()
        self.tool = "ldd"
        self.lib_re_list = [ "^\tlinux-vdso",
                            "^\t/lib64/",
                            "^\t/opt/yb-build/brew/linuxbrew",
                            "^\tstatically linked",
                            "^\tnot a dynamic executable",
                            "ldd: warning: you do not have execution permission",
                            "^.* => /lib64/",
                            "^.* => /lib/",
                            "^.* => /usr/lib/x86_64-linux-gnu/",
                            "^.* => /opt/yb-build/brew/linuxbrew",
                            f"^.* => {self.tp_dir}"]

    def good_libs(self, file_path):
        try:
            libout = subprocess.check_output(['ldd', file_path],
                                             stderr=subprocess.STDOUT, env={'LC_ALL': 'en_US.UTF-8'}).decode('utf-8')
        except subprocess.CalledProcessError as ex:
            if ex.returncode > 1:
                log("Unexpected exit code %d from ldd, file %s", ex.returncode, file_path)
                log(ex.stdout.decode('utf-8'))
                return False
            else:
                libout = ex.stdout.decode('utf-8')
        return self.check_lib_deps(file_path, libout)


def main():
    unset_if_set('CC')
    unset_if_set('CXX')

    if 'YB_BUILD_THIRDPARTY_DUMP_ENV' in os.environ:
        heading('Environment of {}:'.format(sys.argv[0]))
        for key in os.environ:
            log('{}={}'.format(key, os.environ[key]))
        log_separator()

    builder = Builder()
    builder.init()
    builder.run()

    if is_mac():
        tester = LibTestMac()
    elif is_linux():
        tester = LibTestLinux()
    else:
        fatal(f"Unsupported platform: {platform.system()}")
    tester.run()


if __name__ == "__main__":
    main()
