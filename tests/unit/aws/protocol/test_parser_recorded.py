import io
import os
import pickle
import pprint
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from typing import Callable, Dict, TypedDict, Union
from urllib.parse import urlsplit

import botocore.client
import pytest
from botocore.awsrequest import AWSPreparedRequest

from localstack.aws.api import HttpRequest, ServiceRequest
from localstack.aws.protocol.parser import create_parser
from localstack.aws.spec import load_service
from localstack.utils.aws import aws_stack
from localstack.utils.common import to_str


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
    service = get_service_model(api_call["service"])

    parser = get_parser(api_call["service"])
    op, parsed = parser.parse(http_request)

    assert op.name == api_call["operation"]

    # align parsed parameters
    if parsed is None:
        parsed = {}
    walk(
        parsed, delete_none_walker
    )  # Remove None fields from the parsed dict (on purpose for our implementations)
    walk(parsed, delete_utc_walker)  # Remove the timezone from the parsed dict (added on purpose)

    # align the recorded parameters
    recorded = api_call["params"]
    walk(recorded, convert_bytes_io)
    if service.protocol == "query":
        # Remove empty fields if it's the query protocol, they aren't serialized by botocore
        walk(recorded, delete_empty_walker)

    assert parsed == recorded


def delete_utc_walker(target: Union[dict, list], key: any, value: any) -> bool:
    if isinstance(value, datetime):
        fixed_datetime = value.replace(tzinfo=None)
        if isinstance(target, dict):
            target[key] = fixed_datetime
        elif isinstance(target, list):
            for i, value_in_list in enumerate(target):
                if value == value_in_list:
                    target[i] = fixed_datetime
    return True


def convert_bytes_io(target: Union[dict, list], key: any, value: any) -> bool:
    if isinstance(value, io.BytesIO):
        data = value.read()
        if isinstance(target, dict):
            target[key] = data
        elif isinstance(target, list):
            for i, value_in_list in enumerate(target):
                if value == value_in_list:
                    target[i] = data
    return True


def delete_empty_walker(target: Union[dict, list], key: any, value: any) -> bool:
    if not isinstance(value, (dict, list)) or len(value) > 0:
        return True
    if isinstance(target, dict):
        del target[key]
        return False
    elif isinstance(target, list):
        target.remove(value)
        return False
    return True


def delete_none_walker(target: Union[dict, list], key: any, value: any) -> bool:
    if isinstance(target, dict) and value is None:
        del target[key]
        return False
    elif isinstance(target, list) and value is None:
        target.remove(value)
        return False
    return True


def walk(target: Union[dict, list], fct: Callable) -> None:
    if isinstance(target, dict):
        for key, value in list(target.items()):
            if fct(target, key, value):
                walk(value, fct)
    elif isinstance(target, list):
        for item in target:
            if fct(target, None, item):
                walk(item, fct)


def delete_none(target: Union[dict, list]) -> None:
    """Delete None elements in-place recursively in dicts and lists."""
    if isinstance(target, dict):
        for key, value in list(target.items()):
            if value is None:
                del target[key]
            else:
                delete_none(value)
    elif isinstance(target, list):
        for item in target:
            if item is None:
                target.remove(item)
            else:
                delete_none(item)


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
    headers = {}
    for k, v in aws_request.headers.items():
        headers[k] = to_str(v)

    body = aws_request.body
    # If we have a BytesIO body, we convert it to a bytes-like object
    if isinstance(body, io.BytesIO):
        body = body.read()

    return HttpRequest(
        method=aws_request.method,
        path=split_url.path,
        query_string=split_url.query,
        headers=headers,
        body=body,
    )
