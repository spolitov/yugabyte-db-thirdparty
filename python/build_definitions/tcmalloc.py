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

from yugabyte_db_thirdparty.build_definition_helpers import *  # noqa


class TCMallocDependency(Dependency):
    def __init__(self) -> None:
        super(TCMallocDependency, self).__init__(
            name='tcmalloc',
            version='3f80d4dde4dfe7844ece6b307f5dca0ee31ef663',
            url_pattern='https://github.com/google/tcmalloc/archive/{0}.zip',
            build_group=BUILD_GROUP_INSTRUMENTED)
        self.copy_sources = False

    def build(self, builder: BuilderInterface) -> None:
        builder.build_with_cmake(
            self,
            extra_args=[
                '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_POSITION_INDEPENDENT_CODE=On',
                '-DCMAKE_CXX_STANDARD=20',
                '-DCMAKE_INCLUDE_PATH={}/include'.format(builder.prefix),
                '-DCMAKE_INSTALL_PREFIX={}'.format(builder.prefix)
                            ]
        )