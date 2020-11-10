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

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_definitions import *

class TCMallocDependency(Dependency):
    def __init__(self):
        super(TCMallocDependency, self).__init__(
                'tcmalloc', '2020_11_10', 'https://github.com/spolitov/tcmalloc/archive/master.zip',
                BUILD_GROUP_INSTRUMENTED)
        self.copy_sources = False

    def build(self, builder):
        builder.build_with_cmake(self,
                                 ['-DCMAKE_BUILD_TYPE=Release',
                                  '-DCMAKE_POSITION_INDEPENDENT_CODE=On',
                                  '-DCMAKE_CXX_STANDARD=14',
                                  '-DCMAKE_INCLUDE_PATH={}/include'.format(builder.prefix),
                                  '-DCMAKE_INSTALL_PREFIX={}'.format(builder.prefix)])