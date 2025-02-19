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
"""Setup module for the GRPC Python package's optional reflection."""

import os
import sys

import setuptools

# Ensure we're in the proper directory whether or not we're being used by pip.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Break import-style to ensure we can actually find our local modules.
import grpc_version


class _NoOpCommand(setuptools.Command):
    """No-op command."""

    description = ""
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        pass


CLASSIFIERS = [
    "Development Status :: 5 - Production/Stable",
    "Programming Language :: Python",
    "Programming Language :: Python :: 2",
    "Programming Language :: Python :: 2.7",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.4",
    "Programming Language :: Python :: 3.5",
    "Programming Language :: Python :: 3.6",
    "License :: OSI Approved :: Apache Software License",
]

PACKAGE_DIRECTORIES = {
    "": ".",
}

INSTALL_REQUIRES = (
    "protobuf>=3.5.2.post1",
    "grpcio>={version}".format(version=grpc_version.VERSION),
)

try:
    import reflection_commands as _reflection_commands

    # we are in the build environment, otherwise the above import fails
    SETUP_REQUIRES = ("grpcio-tools=={version}".format(version=grpc_version.VERSION),)
    COMMAND_CLASS = {
        # Run preprocess from the repository *before* doing any packaging!
        "preprocess": _reflection_commands.CopyProtoModules,
        "build_package_protos": _reflection_commands.BuildPackageProtos,
    }
except ImportError:
    SETUP_REQUIRES = ()
    COMMAND_CLASS = {
        # wire up commands to no-op not to break the external dependencies
        "preprocess": _NoOpCommand,
        "build_package_protos": _NoOpCommand,
    }

setuptools.setup(
    name="grpcio-reflection",
    version=grpc_version.VERSION,
    license="Apache License 2.0",
    description="Standard Protobuf Reflection Service for gRPC",
    author="The gRPC Authors",
    author_email="grpc-io@googlegroups.com",
    classifiers=CLASSIFIERS,
    url="https://grpc.io",
    package_dir=PACKAGE_DIRECTORIES,
    packages=setuptools.find_packages("."),
    install_requires=INSTALL_REQUIRES,
    setup_requires=SETUP_REQUIRES,
    cmdclass=COMMAND_CLASS,
)
