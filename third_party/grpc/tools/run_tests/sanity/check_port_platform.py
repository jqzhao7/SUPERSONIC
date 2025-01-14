#!/usr/bin/env python

# Copyright 2017 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys

os.chdir(os.path.join(os.path.dirname(sys.argv[0]), "../../.."))


def check_port_platform_inclusion(directory_root):
    bad_files = []
    for root, dirs, files in os.walk(directory_root):
        for filename in files:
            path = os.path.join(root, filename)
            if os.path.splitext(path)[1] not in [".c", ".cc", ".h"]:
                continue
            if path in [
                os.path.join("include", "grpc", "support", "port_platform.h"),
                os.path.join("include", "grpc", "impl", "codegen", "port_platform.h"),
            ]:
                continue
            if filename.endswith(".pb.h") or filename.endswith(".pb.c"):
                continue
            with open(path) as f:
                all_lines_in_file = f.readlines()
                for index, l in enumerate(all_lines_in_file):
                    if "#include" in l:
                        if l not in [
                            "#include <grpc/support/port_platform.h>\n",
                            "#include <grpc/impl/codegen/port_platform.h>\n",
                        ]:
                            bad_files.append(path)
                        elif all_lines_in_file[index + 1] != "\n":
                            # Require a blank line after including port_platform.h in
                            # order to prevent the formatter from reording it's
                            # inclusion order upon future changes.
                            bad_files.append(path)
                        break
    return bad_files


all_bad_files = []
all_bad_files += check_port_platform_inclusion(os.path.join("src", "core"))
all_bad_files += check_port_platform_inclusion(os.path.join("include", "grpc"))

if len(all_bad_files) > 0:
    for f in all_bad_files:
        print(
            (
                "port_platform.h is not the first included header or there "
                "is not a blank line following its inclusion in %s"
            )
            % f
        )
    sys.exit(1)
