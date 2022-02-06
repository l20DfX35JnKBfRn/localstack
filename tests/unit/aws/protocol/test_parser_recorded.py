import os
import pickle
import pprint
from collections import defaultdict
from functools import lru_cache
from typing import Dict, TypedDict
from urllib.parse import urlsplit

import botocore.client
import pytest
from botocore.awsrequest import AWSPreparedRequest
from werkzeug.datastructures import Headers

from localstack.aws.api import HttpRequest, ServiceRequest
from localstack.aws.protocol.parser import create_parser
from localstack.aws.spec import load_service
from localstack.utils.aws import aws_stack


class ApiCall(TypedDict):
    service: str
    operation: str
    params: ServiceRequest


class HttpRequestDict(TypedDict):
    url: str
    url_path: str
    query_string: str
    method: str
    streaming: bool
    headers: Dict[str, bytes]
    body: bytes


@lru_cache()
def get_parser(service: str):
    return create_parser(get_service_model(service))


@lru_cache()
def get_service_model(service: str):
    return load_service(service)


@lru_cache()
def get_client(service: str) -> botocore.client.BaseClient:
    return aws_stack.connect_to_service(service)


def load_test_cases():
    d = os.path.join(__file__, "../../../../../target/api-calls")
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
def test_parse_request(api_call: ApiCall):
    print()
    print(f"{api_call['service']}.{api_call['operation']}")
    pprint.pprint(api_call["params"])

    # this emulates what boto does
    aws_request = to_aws_request(api_call)
    http_request = to_http_request(aws_request)

    parser = get_parser(api_call["service"])
    op, parsed = parser.parse(http_request)

    assert op.name == api_call["operation"]
    # align parsed and recorded parameters
    # TODO: more alignment will have to be done)
    if parsed is None:
        parsed = {}
    assert parsed == api_call["params"]


def to_aws_request(api_call: ApiCall) -> AWSPreparedRequest:
    # this is from the guts of boto, where boto **kwarg dicts are converted to HTTP requests
    client = get_client(api_call["service"])
    operation_model = client._service_model.operation_model(api_call["operation"])
    request_context = {
        "client_region": client.meta.region_name,
        "client_config": client.meta.config,
        "has_streaming_input": operation_model.has_streaming_input,
        "auth_type": operation_model.auth_type,
    }
    request_dict = client._convert_to_request_dict(
        api_call["params"], operation_model, context=request_context
    )
    request = client._endpoint.create_request(request_dict, operation_model)
    return request


def to_http_request(aws_request: AWSPreparedRequest) -> HttpRequest:
    split_url = urlsplit(aws_request.url)
    headers = Headers()
    for k, v in aws_request.headers.items():
        headers[k] = v

    return HttpRequest(
        method=aws_request.method,
        path=split_url.path,
        query_string=split_url.query,
        headers=headers,
        body=aws_request.body,
    )
