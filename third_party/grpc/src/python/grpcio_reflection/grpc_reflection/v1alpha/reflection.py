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
"""Reference implementation for reflection in gRPC Python."""

import grpc
from google.protobuf import descriptor_pb2
from google.protobuf import descriptor_pool

from grpc_reflection.v1alpha import reflection_pb2
from grpc_reflection.v1alpha import reflection_pb2_grpc

_POOL = descriptor_pool.Default()


def _not_found_error():
    return reflection_pb2.ServerReflectionResponse(
        error_response=reflection_pb2.ErrorResponse(
            error_code=grpc.StatusCode.NOT_FOUND.value[0],
            error_message=grpc.StatusCode.NOT_FOUND.value[1].encode(),
        )
    )


def _file_descriptor_response(descriptor):
    proto = descriptor_pb2.FileDescriptorProto()
    descriptor.CopyToProto(proto)
    serialized_proto = proto.SerializeToString()
    return reflection_pb2.ServerReflectionResponse(
        file_descriptor_response=reflection_pb2.FileDescriptorResponse(
            file_descriptor_proto=(serialized_proto,)
        ),
    )


class ReflectionServicer(reflection_pb2_grpc.ServerReflectionServicer):
    """Servicer handling RPCs for service statuses."""

    def __init__(self, service_names, pool=None):
        """Constructor.

    Args:
      service_names: Iterable of fully-qualified service names available.
    """
        self._service_names = tuple(sorted(service_names))
        self._pool = _POOL if pool is None else pool

    def _file_by_filename(self, filename):
        try:
            descriptor = self._pool.FindFileByName(filename)
        except KeyError:
            return _not_found_error()
        else:
            return _file_descriptor_response(descriptor)

    def _file_containing_symbol(self, fully_qualified_name):
        try:
            descriptor = self._pool.FindFileContainingSymbol(fully_qualified_name)
        except KeyError:
            return _not_found_error()
        else:
            return _file_descriptor_response(descriptor)

    def _file_containing_extension(self, containing_type, extension_number):
        try:
            message_descriptor = self._pool.FindMessageTypeByName(containing_type)
            extension_descriptor = self._pool.FindExtensionByNumber(
                message_descriptor, extension_number
            )
            descriptor = self._pool.FindFileContainingSymbol(
                extension_descriptor.full_name
            )
        except KeyError:
            return _not_found_error()
        else:
            return _file_descriptor_response(descriptor)

    def _all_extension_numbers_of_type(self, containing_type):
        try:
            message_descriptor = self._pool.FindMessageTypeByName(containing_type)
            extension_numbers = tuple(
                sorted(
                    extension.number
                    for extension in self._pool.FindAllExtensions(message_descriptor)
                )
            )
        except KeyError:
            return _not_found_error()
        else:
            return reflection_pb2.ServerReflectionResponse(
                all_extension_numbers_response=reflection_pb2.ExtensionNumberResponse(
                    base_type_name=message_descriptor.full_name,
                    extension_number=extension_numbers,
                )
            )

    def _list_services(self):
        return reflection_pb2.ServerReflectionResponse(
            list_services_response=reflection_pb2.ListServiceResponse(
                service=[
                    reflection_pb2.ServiceResponse(name=service_name)
                    for service_name in self._service_names
                ]
            )
        )

    def ServerReflectionInfo(self, request_iterator, context):
        # pylint: disable=unused-argument
        for request in request_iterator:
            if request.HasField("file_by_filename"):
                yield self._file_by_filename(request.file_by_filename)
            elif request.HasField("file_containing_symbol"):
                yield self._file_containing_symbol(request.file_containing_symbol)
            elif request.HasField("file_containing_extension"):
                yield self._file_containing_extension(
                    request.file_containing_extension.containing_type,
                    request.file_containing_extension.extension_number,
                )
            elif request.HasField("all_extension_numbers_of_type"):
                yield self._all_extension_numbers_of_type(
                    request.all_extension_numbers_of_type
                )
            elif request.HasField("list_services"):
                yield self._list_services()
            else:
                yield reflection_pb2.ServerReflectionResponse(
                    error_response=reflection_pb2.ErrorResponse(
                        error_code=grpc.StatusCode.INVALID_ARGUMENT.value[0],
                        error_message=grpc.StatusCode.INVALID_ARGUMENT.value[
                            1
                        ].encode(),
                    )
                )


def enable_server_reflection(service_names, server, pool=None):
    """Enables server reflection on a server.

    Args:
      service_names: Iterable of fully-qualified service names available.
      server: grpc.Server to which reflection service will be added.
      pool: DescriptorPool object to use (descriptor_pool.Default() if None).
    """
    reflection_pb2_grpc.add_ServerReflectionServicer_to_server(
        ReflectionServicer(service_names, pool=pool), server
    )
