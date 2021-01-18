# Copyright (c) Yugabyte, Inc.
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

import os
import sys
import re
import subprocess
import platform
import logging

from typing import List, Any, Set, Optional, Pattern
from yugabyte_db_thirdparty.os_detection import is_mac, is_linux
from yugabyte_db_thirdparty.custom_logging import log, fatal, heading
from yugabyte_db_thirdparty.util import YB_THIRDPARTY_DIR
from build_definitions import BUILD_TYPES


def compile_re_list(re_list: List[str]) -> Any:
    return re.compile("|".join(re_list))


class LibTestBase:
    """
    Verify correct library paths are used in installed dynamically-linked executables and
    libraries.
    """

    lib_re_list: List[str]
    tool: str

    def __init__(self) -> None:
        self.tp_installed_dir = os.path.join(YB_THIRDPARTY_DIR, 'installed')
        self.lib_re_list = []

    def init_regex(self) -> None:
        self.okay_paths = compile_re_list(self.lib_re_list)

    def check_lib_deps(
            self,
            file_path: str,
            cmdout: str,
            additional_allowed_pattern: Optional[Pattern] = None) -> bool:

        status = True
        for line in cmdout.splitlines():
            if (not self.okay_paths.match(line) and
                    not (additional_allowed_pattern and
                         additional_allowed_pattern.match(line))):
                if status:
                    log(file_path + ":")
                    status = False
                log("Bad path: %s", line)
        return status

    # overridden in platform specific classes
    def good_libs(self, file_path: str) -> bool:
        raise NotImplementedError()

    def run(self) -> None:
        self.init_regex()
        heading("Scanning installed executables and libraries...")
        test_pass = True
        # files to examine are much reduced if we look only at bin and lib directories
        dir_pattern = re.compile('^(lib|libcxx|[s]bin)$')
        dirs = [os.path.join(self.tp_installed_dir, type) for type in BUILD_TYPES]
        for installed_dir in dirs:
            if not os.path.isdir(installed_dir):
                logging.info("Directory %s does not exist, skipping", installed_dir)
                continue
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

    def add_allowed_shared_lib_paths(self, shared_lib_paths: Set[str]) -> None:
        pass


class LibTestMac(LibTestBase):
    def __init__(self) -> None:
        super().__init__()
        self.tool = "otool -L"
        self.lib_re_list = ["^\t/usr/",
                            "^\t/System/Library/",
                            "^Archive ",
                            "^/",
                            "^\t@rpath",
                            "^\t@loader_path",
                            f"^\t{YB_THIRDPARTY_DIR}"]

    def add_allowed_shared_lib_paths(self, shared_lib_paths: Set[str]) -> None:
        # TODO: implement this on macOS for more precise checking of allowed dylib paths.
        pass

    def good_libs(self, file_path: str) -> bool:
        libout = subprocess.check_output(['otool', '-L', file_path]).decode('utf-8')
        if 'is not an object file' in libout:
            return True
        return self.check_lib_deps(file_path, libout)


class LibTestLinux(LibTestBase):
    LIBCXX_NOT_FOUND = re.compile('^\tlibc[+][+][.]so[.][0-9]+ => not found')

    def __init__(self) -> None:
        super().__init__()
        self.tool = "ldd"
        self.lib_re_list = [
            "^\tlinux-vdso",
            "^\t/lib64/",
            "^\t/opt/yb-build/brew/linuxbrew",
            "^\tstatically linked",
            "^\tnot a dynamic executable",
            "ldd: warning: you do not have execution permission",
            "^.* => /lib64/",
            "^.* => /lib/",
            "^.* => /usr/lib/x86_64-linux-gnu/",
            "^.* => /opt/yb-build/brew/linuxbrew",
            f"^.* => {re.escape(YB_THIRDPARTY_DIR)}"
        ]

    def add_allowed_shared_lib_paths(self, shared_lib_paths: Set[str]) -> None:
        for shared_lib_path in sorted(shared_lib_paths):
            self.lib_re_list.append(f".* => {re.escape(shared_lib_path)}/")

    def good_libs(self, file_path: str) -> bool:
        try:
            libout = subprocess.check_output(
                ['ldd', file_path],
                stderr=subprocess.STDOUT, env={'LC_ALL': 'en_US.UTF-8'}).decode('utf-8')
        except subprocess.CalledProcessError as ex:
            if ex.returncode > 1:
                log("Unexpected exit code %d from ldd, file %s", ex.returncode, file_path)
                log(ex.stdout.decode('utf-8'))
                return False

            libout = ex.stdout.decode('utf-8')

        file_basename = os.path.basename(file_path)
        additional_allowed_pattern = None
        if file_basename.startswith('libc++abi.so.'):
            # One exception: libc++abi.so is not able to find libc++ because it loads the ASAN
            # runtime library that is part of the LLVM distribution and does not have the correct
            # rpath set. This happens on CentOS with our custom build of LLVM. We might be able to
            # fix this by specifyng rpath correctly when building LLVM, but as of 12/2020 we just
            # ignore this error here.
            #
            # $ ldd installed/asan/libcxx/lib/libc++abi.so.1.0
            #   linux-vdso.so.1 =>
            #   libclang_rt.asan-x86_64.so =>
            #     $LLVM_DIR/lib/clang/11.0.0/lib/linux/libclang_rt.asan-x86_64.so
            #   libclang_rt.ubsan_minimal-x86_64.so =>
            #     $LLVM_DIR/lib/clang/11.0.0/lib/linux/libclang_rt.ubsan_minimal-x86_64.so
            #   libunwind.so.1 => installed/common/lib/libunwind.so.1
            #   libdl.so.2 => /lib64/libdl.so.2
            #   libpthread.so.0 => /lib64/libpthread.so.0
            #   libm.so.6 => /lib64/libm.so.6
            #   libc.so.6 => /lib64/libc.so.6
            #   libc++.so.1 => not found  <-- THIS IS OK
            #   libgcc_s.so.1 => /lib64/libgcc_s.so.1
            #   librt.so.1 => /lib64/librt.so.1
            #   /lib64/ld-linux-x86-64.so.2
            #
            # Run
            #   LD_DEBUG=all ldd installed/asan/libcxx/lib/libc++abi.so.1.0
            # and notice the following line:
            #
            # file=libc++.so.1 [0];
            #   needed by $LLVM_DIR/lib/clang/11.0.0/lib/linux/libclang_rt.asan-x86_64.so
            #
            # Also running
            #   ldd $LLVM_DIR/lib/clang/11.0.0/lib/linux/libclang_rt.asan-x86_64.so
            #
            # reports "libc++.so.1 => not found".
            additional_allowed_pattern = self.LIBCXX_NOT_FOUND

        return self.check_lib_deps(file_path, libout, additional_allowed_pattern)


def get_lib_tester() -> LibTestBase:
    if is_mac():
        return LibTestMac()
    if is_linux():
        return LibTestLinux()

    fatal(f"Unsupported platform: {platform.system()}")
