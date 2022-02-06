import copy
import os
import pickle
import pprint
from collections import defaultdict
from functools import lru_cache
from typing import Dict, TypedDict

import botocore.client
import pytest
from botocore.parsers import create_parser

from localstack.aws.protocol.serializer import create_serializer
from localstack.aws.spec import load_service
from localstack.utils.aws import aws_stack


class ApiResponse(TypedDict):
    service: str
    operation: str
    params: Dict  # parsed response


class HttpRequestDict(TypedDict):
    url: str
    url_path: str
    query_string: str
    method: str
    streaming: bool
    headers: Dict[str, bytes]
    body: bytes


@lru_cache()
def get_service_model(service: str):
    return load_service(service)


@lru_cache()
def get_client(service: str) -> botocore.client.BaseClient:
    return aws_stack.connect_to_service(service)


def load_test_cases():
    d = os.path.join(__file__, "../../../../../target/responses")
    d = os.path.abspath(d)

    if not os.path.exists(d):
        return

    call_counters = defaultdict(lambda: 0)

    for f in os.listdir(d):
        with open(os.path.join(d, f), "rb") as fd:
            try:
                records = pickle.load(fd)
            except Exception as e:
                print(e)
                continue

        for record in records:
            service = get_service_model(record["service"])
            key = f"{service.protocol}_{record['service']}_{record['operation']}"
            i = call_counters[key]
            call_counters[key] += 1

            test_id = f"{key}_{i:03d}"
            yield test_id, record


# collect test cases
test_cases = list(load_test_cases())
test_cases.sort(key=lambda x: x[0])
test_ids = [x[0] for x in test_cases]
test_params = [x[1] for x in test_cases]


@pytest.mark.parametrize("api_call", test_params, ids=test_ids)
def test_serialize_response(api_call: ApiResponse):
    response = api_call["params"]

    print()
    print(f"{api_call['service']}.{api_call['operation']}")

    if response["ResponseMetadata"]["HTTPStatusCode"] >= 400:
        # TODO implement error serialization
        pytest.skip("TODO: implement error serialization")
        return

    # status_code = response["ResponseMetadata"]["HTTPStatusCode"]

    data = copy.deepcopy(response)
    del data["ResponseMetadata"]
    pprint.pprint(data)

    # remaining code copied from __botocore_serializer_integration_test

    # Load the appropriate service
    service = load_service(api_call["service"])
    operation = api_call["operation"]

    # Use our serializer to serialize the response
    response_serializer = create_serializer(service)
    serialized_response = response_serializer.serialize_to_response(
        data, service.operation_model(operation)
    )

    # Use the parser from botocore to parse the serialized response
    response_parser = create_parser(service.protocol)
    parsed_response = response_parser.parse(
        serialized_response.to_readonly_response_dict(),
        service.operation_model(operation).output_shape,
    )

    # Check if the result is equal to the initial response params
    assert "ResponseMetadata" in parsed_response
    assert "HTTPStatusCode" in parsed_response["ResponseMetadata"]
    # FIXME: moto/localstack has pretty much ignored 201/202/204, ... response codes so this will fail often
    # assert parsed_response["ResponseMetadata"]["HTTPStatusCode"] == status_code
    assert "RequestId" in parsed_response["ResponseMetadata"]
    assert len(parsed_response["ResponseMetadata"]["RequestId"]) == 52
    del parsed_response["ResponseMetadata"]
    assert parsed_response == data
