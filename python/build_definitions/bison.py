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


class BisonDependency(Dependency):
    def __init__(self) -> None:
        super(BisonDependency, self).__init__(
            name='bison',
            version='3.4.1',
            url_pattern='https://ftp.gnu.org/gnu/bison/bison-{0}.tar.gz',
            build_group=BUILD_GROUP_COMMON,
            license='GPL-3.0')
        self.copy_sources = True

    def build(self, builder: BuilderInterface) -> None:
        builder.build_with_configure(
            log_prefix=builder.log_prefix(self),
            extra_args=['--with-pic']
        )
