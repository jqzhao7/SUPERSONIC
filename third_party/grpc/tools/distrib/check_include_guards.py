#!/usr/bin/env python2.7

# Copyright 2016 gRPC authors.
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

import argparse
import os
import os.path
import re
import sys
import subprocess


def build_valid_guard(fpath):
    prefix = "GRPC_" if not fpath.startswith("include/") else ""
    return prefix + "_".join(
        fpath.replace("++", "XX").replace(".", "_").upper().split("/")[1:]
    )


def load(fpath):
    with open(fpath, "r") as f:
        return f.read()


def save(fpath, contents):
    with open(fpath, "w") as f:
        f.write(contents)


class GuardValidator(object):
    def __init__(self):
        self.ifndef_re = re.compile(r"#ifndef ([A-Z][A-Z_1-9]*)")
        self.define_re = re.compile(r"#define ([A-Z][A-Z_1-9]*)")
        self.endif_c_re = re.compile(r"#endif /\* ([A-Z][A-Z_1-9]*) (?:\\ *\n *)?\*/")
        self.endif_cpp_re = re.compile(r"#endif  // ([A-Z][A-Z_1-9]*)")
        self.failed = False

    def fail(self, fpath, regexp, fcontents, match_txt, correct, fix):
        cpp_header = "grpc++" in fpath or "grpcpp" in fpath
        self.failed = True
        invalid_guards_msg_template = (
            "{0}: Missing preprocessor guards (RE {1}). "
            "Please wrap your code around the following guards:\n"
            "#ifndef {2}\n"
            "#define {2}\n"
            "...\n"
            "... epic code ...\n"
            "...\n"
        ) + ("#endif  // {2}" if cpp_header else "#endif /* {2} */")
        if not match_txt:
            print invalid_guards_msg_template.format(
                fpath, regexp.pattern, build_valid_guard(fpath)
            )
            return fcontents

        print (
            "{}: Wrong preprocessor guards (RE {}):" "\n\tFound {}, expected {}"
        ).format(fpath, regexp.pattern, match_txt, correct)
        if fix:
            print "Fixing {}...\n".format(fpath)
            fixed_fcontents = re.sub(match_txt, correct, fcontents)
            if fixed_fcontents:
                self.failed = False
            return fixed_fcontents
        else:
            print
        return fcontents

    def check(self, fpath, fix):
        cpp_header = "grpc++" in fpath or "grpcpp" in fpath
        valid_guard = build_valid_guard(fpath)

        fcontents = load(fpath)

        match = self.ifndef_re.search(fcontents)
        if not match:
            print "something drastically wrong with: %s" % fpath
            return False  # failed
        if match.lastindex is None:
            # No ifndef. Request manual addition with hints
            self.fail(fpath, match.re, match.string, "", "", False)
            return False  # failed

        # Does the guard end with a '_H'?
        running_guard = match.group(1)
        if not running_guard.endswith("_H"):
            fcontents = self.fail(
                fpath, match.re, match.string, match.group(1), valid_guard, fix
            )
            if fix:
                save(fpath, fcontents)

        # Is it the expected one based on the file path?
        if running_guard != valid_guard:
            fcontents = self.fail(
                fpath, match.re, match.string, match.group(1), valid_guard, fix
            )
            if fix:
                save(fpath, fcontents)

        # Is there a #define? Is it the same as the #ifndef one?
        match = self.define_re.search(fcontents)
        if match.lastindex is None:
            # No define. Request manual addition with hints
            self.fail(fpath, match.re, match.string, "", "", False)
            return False  # failed

        # Is the #define guard the same as the #ifndef guard?
        if match.group(1) != running_guard:
            fcontents = self.fail(
                fpath, match.re, match.string, match.group(1), valid_guard, fix
            )
            if fix:
                save(fpath, fcontents)

        # Is there a properly commented #endif?
        endif_re = self.endif_cpp_re if cpp_header else self.endif_c_re
        flines = fcontents.rstrip().splitlines()
        match = endif_re.search("\n".join(flines[-2:]))
        if not match:
            # No endif. Check if we have the last line as just '#endif' and if so
            # replace it with a properly commented one.
            if flines[-1] == "#endif":
                flines[-1] = "#endif" + (
                    "  // {}\n".format(valid_guard)
                    if cpp_header
                    else " /* {} */\n".format(valid_guard)
                )
                if fix:
                    fcontents = "\n".join(flines)
                    save(fpath, fcontents)
            else:
                # something else is wrong, bail out
                self.fail(fpath, endif_re, flines[-1], "", "", False)
        elif match.group(1) != running_guard:
            # Is the #endif guard the same as the #ifndef and #define guards?
            fcontents = self.fail(
                fpath, endif_re, fcontents, match.group(1), valid_guard, fix
            )
            if fix:
                save(fpath, fcontents)

        return not self.failed  # Did the check succeed? (ie, not failed)


# find our home
ROOT = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), "../.."))
os.chdir(ROOT)

# parse command line
argp = argparse.ArgumentParser(description="include guard checker")
argp.add_argument("-f", "--fix", default=False, action="store_true")
argp.add_argument("--precommit", default=False, action="store_true")
args = argp.parse_args()

KNOWN_BAD = set(
    [
        "src/core/ext/filters/client_channel/lb_policy/grpclb/proto/grpc/lb/v1/load_balancer.pb.h",
        "src/core/tsi/alts/handshaker/altscontext.pb.h",
        "src/core/tsi/alts/handshaker/handshaker.pb.h",
        "src/core/tsi/alts/handshaker/transport_security_common.pb.h",
        "include/grpc++/ext/reflection.grpc.pb.h",
        "include/grpc++/ext/reflection.pb.h",
    ]
)

grep_filter = r"grep -E '^(include|src/core)/.*\.h$'"
if args.precommit:
    git_command = "git diff --name-only HEAD"
else:
    git_command = "git ls-tree -r --name-only -r HEAD"

FILE_LIST_COMMAND = " | ".join((git_command, grep_filter))

# scan files
ok = True
filename_list = []
try:
    filename_list = subprocess.check_output(FILE_LIST_COMMAND, shell=True).splitlines()
    # Filter out non-existent files (ie, file removed or renamed)
    filename_list = (f for f in filename_list if os.path.isfile(f))
except subprocess.CalledProcessError:
    sys.exit(0)

validator = GuardValidator()

for filename in filename_list:
    if filename in KNOWN_BAD:
        continue
    ok = ok and validator.check(filename, args.fix)

sys.exit(0 if ok else 1)
