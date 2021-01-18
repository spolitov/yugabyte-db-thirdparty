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
from typing import Optional, Tuple

from build_definitions import (
    BUILD_TYPE_ASAN,
    BUILD_TYPE_TSAN,
    BUILD_TYPE_CLANG_UNINSTRUMENTED,
    BUILD_TYPE_UNINSTRUMENTED
)
from yugabyte_db_thirdparty.custom_logging import fatal
from yugabyte_db_thirdparty.os_detection import is_linux, is_mac
from yugabyte_db_thirdparty.util import (
    which_must_exist,
    YB_THIRDPARTY_DIR,
    add_path_entry,
)


class CompilerChoice:
    compiler_type: str
    cc: Optional[str]
    cxx: Optional[str]
    single_compiler_type: Optional[str]
    compiler_prefix: Optional[str]
    compiler_suffix: str
    devtoolset: Optional[int]
    linuxbrew_dir: Optional[str]
    use_compiler_wrapper: bool
    use_ccache: bool

    def __init__(
            self,
            single_compiler_type: Optional[str],
            compiler_prefix: Optional[str],
            compiler_suffix: str,
            devtoolset: Optional[int],
            use_compiler_wrapper: bool,
            use_ccache: bool) -> None:
        self.single_compiler_type = single_compiler_type
        self.compiler_prefix = compiler_prefix
        self.compiler_suffix = compiler_suffix
        self.devtoolset = devtoolset
        self.use_compiler_wrapper = use_compiler_wrapper
        self.use_ccache = use_ccache

    def detect_linuxbrew(self) -> None:
        if (not is_linux() or
                self.single_compiler_type or
                self.compiler_prefix or
                self.compiler_suffix):
            self.linuxbrew_dir = None
            return

        self.linuxbrew_dir = os.getenv('YB_LINUXBREW_DIR')

        if self.linuxbrew_dir:
            add_path_entry(os.path.join(self.linuxbrew_dir, 'bin'))

    def using_linuxbrew(self) -> bool:
        return self.linuxbrew_dir is not None

    def get_linuxbrew_dir(self) -> str:
        assert self.linuxbrew_dir is not None
        return self.linuxbrew_dir

    def find_compiler_by_type(self, compiler_type: str) -> None:
        compilers: Tuple[str, str]
        if compiler_type == 'gcc':
            if self.use_only_clang():
                raise ValueError('Not allowed to use GCC')
            compilers = self.find_gcc()
        elif compiler_type == 'clang':
            if self.use_only_gcc():
                raise ValueError('Not allowed to use Clang')
            compilers = self.find_clang()
        else:
            fatal("Unknown compiler type {}".format(compiler_type))
        assert len(compilers) == 2

        for compiler in compilers:
            if compiler is None or not os.path.exists(compiler):
                fatal("Compiler executable does not exist: {}".format(compiler))

        self.cc = compilers[0]
        self.validate_compiler_path(self.cc)
        self.cxx = compilers[1]
        self.validate_compiler_path(self.cxx)

    def validate_compiler_path(self, compiler_path: str) -> None:
        if self.devtoolset:
            devtoolset_substring = '/devtoolset-%d/' % self.devtoolset
            if devtoolset_substring not in compiler_path:
                raise ValueError(
                    "Invalid compiler path: %s. Substring not found: %s" % (
                        compiler_path, devtoolset_substring))
        if not os.path.exists(compiler_path):
            raise IOError("Compiler does not exist: %s" % compiler_path)

    def get_c_compiler(self) -> str:
        assert self.cc is not None
        return self.cc

    def get_cxx_compiler(self) -> str:
        assert self.cxx is not None
        return self.cxx

    def find_gcc(self) -> Tuple[str, str]:
        return self.do_find_gcc('gcc', 'g++')

    def do_find_gcc(self, c_compiler: str, cxx_compiler: str) -> Tuple[str, str]:
        if self.using_linuxbrew():
            gcc_dir = self.get_linuxbrew_dir()
        elif self.compiler_prefix:
            gcc_dir = self.compiler_prefix
        else:
            c_compiler_path = which_must_exist(c_compiler)
            cxx_compiler_path = which_must_exist(cxx_compiler)
            return c_compiler_path, cxx_compiler_path

        gcc_bin_dir = os.path.join(gcc_dir, 'bin')

        if not os.path.isdir(gcc_bin_dir):
            fatal("Directory {} does not exist".format(gcc_bin_dir))

        return (os.path.join(gcc_bin_dir, 'gcc') + self.compiler_suffix,
                os.path.join(gcc_bin_dir, 'g++') + self.compiler_suffix)

    def find_clang(self) -> Tuple[str, str]:
        clang_prefix: Optional[str] = None
        if self.compiler_prefix:
            clang_prefix = self.compiler_prefix
        else:
            candidate_dirs = [
                os.path.join(YB_THIRDPARTY_DIR, 'clang-toolchain'),
                '/usr'
            ]
            for dir in candidate_dirs:
                bin_dir = os.path.join(dir, 'bin')
                if os.path.exists(os.path.join(bin_dir, 'clang' + self.compiler_suffix)):
                    clang_prefix = dir
                    break
            if clang_prefix is None:
                fatal("Failed to find clang at the following locations: {}".format(candidate_dirs))

        assert clang_prefix is not None
        clang_bin_dir = os.path.join(clang_prefix, 'bin')

        return (os.path.join(clang_bin_dir, 'clang') + self.compiler_suffix,
                os.path.join(clang_bin_dir, 'clang++') + self.compiler_suffix)

    def building_with_clang(self, build_type: str) -> bool:
        """
        Returns true if we are using clang to build current build_type.
        """
        if self.use_only_clang():
            return True
        if self.use_only_gcc():
            return False

        return build_type in [
            BUILD_TYPE_ASAN,
            BUILD_TYPE_TSAN,
            BUILD_TYPE_CLANG_UNINSTRUMENTED
        ]

    def will_need_clang(self, build_type: str) -> bool:
        """
        Returns true if we will need Clang to complete the full thirdparty build type requested by
        the user.
        """
        if self.use_only_gcc():
            return False
        return build_type != BUILD_TYPE_UNINSTRUMENTED

    def use_only_clang(self) -> bool:
        return is_mac() or self.single_compiler_type == 'clang'

    def use_only_gcc(self) -> bool:
        return self.devtoolset is not None or self.single_compiler_type == 'gcc'

    def is_linux_clang1x(self) -> bool:
        # TODO: actually check compiler version.
        return (
            not is_mac() and
            self.single_compiler_type == 'clang' and
            not self.using_linuxbrew()
        )

    def set_compiler(self, compiler_type: str) -> None:
        if is_mac():
            if compiler_type != 'clang':
                raise ValueError(
                    "Cannot set compiler type to %s on macOS, only clang is supported" %
                    compiler_type)
            self.compiler_type = 'clang'
        else:
            self.compiler_type = compiler_type

        self.find_compiler_by_type(compiler_type)

        c_compiler = self.get_c_compiler()
        cxx_compiler = self.get_cxx_compiler()

        if self.use_compiler_wrapper:
            os.environ['YB_THIRDPARTY_REAL_C_COMPILER'] = c_compiler
            os.environ['YB_THIRDPARTY_REAL_CXX_COMPILER'] = cxx_compiler
            os.environ['YB_THIRDPARTY_USE_CCACHE'] = '1' if self.use_ccache else '0'

            python_scripts_dir = os.path.join(YB_THIRDPARTY_DIR, 'python', 'yugabyte_db_thirdparty')
            os.environ['CC'] = os.path.join(python_scripts_dir, 'compiler_wrapper_cc.py')
            os.environ['CXX'] = os.path.join(python_scripts_dir, 'compiler_wrapper_cxx.py')
        else:
            os.environ['CC'] = c_compiler
            os.environ['CXX'] = cxx_compiler
