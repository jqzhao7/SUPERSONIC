# Copyright 2015 gRPC authors.
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
"""The Python implementation of the GRPC interoperability test server."""

import argparse
from concurrent import futures
import logging
import time

import grpc
from SuperSonic.proto.grpc.testing import test_pb2_grpc

from tests.interop import methods
from tests.interop import resources
from tests.unit import test_common

_ONE_DAY_IN_SECONDS = 60 * 60 * 24


def serve():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port", type=int, required=True, help="the port on which to serve"
    )
    parser.add_argument(
        "--use_tls",
        default=False,
        type=resources.parse_bool,
        help="require a secure connection",
    )
    args = parser.parse_args()

    server = test_common.test_server()
    test_pb2_grpc.add_TestServiceServicer_to_server(methods.TestService(), server)
    if args.use_tls:
        private_key = resources.private_key()
        certificate_chain = resources.certificate_chain()
        credentials = grpc.ssl_server_credentials(((private_key, certificate_chain),))
        server.add_secure_port("[::]:{}".format(args.port), credentials)
    else:
        server.add_insecure_port("[::]:{}".format(args.port))

    server.start()
    logging.info("Server serving.")
    try:
        while True:
            time.sleep(_ONE_DAY_IN_SECONDS)
    except BaseException as e:
        logging.info('Caught exception "%s"; stopping server...', e)
        server.stop(None)
        logging.info("Server stopped; exiting.")


if __name__ == "__main__":
    serve()
