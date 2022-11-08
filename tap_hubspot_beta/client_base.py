"""REST client handling, including hubspotStream base class."""
import copy
import logging

import requests
from typing import Any, Dict, Optional
from backports.cached_property import cached_property
from singer_sdk import typing as th
from singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from singer_sdk.streams import RESTStream
from singer_sdk.helpers.jsonpath import extract_jsonpath
import backoff

from pendulum import parse

from tap_hubspot_beta.auth import OAuth2Authenticator

logging.getLogger("backoff").setLevel(logging.CRITICAL)


class hubspotStream(RESTStream):
    """hubspot stream class."""

    url_base = "https://api.hubapi.com/"
    base_properties = []
    additional_prarams = {}
    properties_url = None
    page_size = 100

    def _request(
        self, prepared_request: requests.PreparedRequest, context: Optional[dict]
    ) -> requests.Response:

        authenticator = self.authenticator
        if authenticator:
            prepared_request.headers.update(authenticator.auth_headers or {})

        response = self.requests_session.send(prepared_request, timeout=self.timeout)
        if self._LOG_REQUEST_METRICS:
            extra_tags = {}
            if self._LOG_REQUEST_METRIC_URLS:
                extra_tags["url"] = prepared_request.path_url
            self._write_request_duration_log(
                endpoint=self.path,
                response=response,
                context=context,
                extra_tags=extra_tags,
            )
        self.validate_response(response)
        logging.debug("Response received successfully.")
        return response

    @cached_property
    def last_job(self):
        if self.tap_state.get("bookmarks"):
            last_job = self.tap_state["bookmarks"].get("last_job")
            if last_job:
                return parse(last_job.get("value"))
        return 

    def request_records(self, context):
        """Request records from REST endpoint(s), returning response records."""
        next_page_token = None
        finished = False
        decorated_request = self.request_decorator(self._request)

        while not finished:
            logging.getLogger("backoff").setLevel(logging.CRITICAL)
            prepared_request = self.prepare_request(
                context, next_page_token=next_page_token
            )
            resp = decorated_request(prepared_request, context)
            for row in self.parse_response(resp):
                yield row
            previous_token = copy.deepcopy(next_page_token)
            next_page_token = self.get_next_page_token(
                response=resp, previous_token=previous_token
            )
            if next_page_token and next_page_token == previous_token:
                raise RuntimeError(
                    f"Loop detected in pagination. "
                    f"Pagination token {next_page_token} is identical to prior token."
                )
            finished = not next_page_token

    @property
    def authenticator(self) -> OAuth2Authenticator:
        """Return a new authenticator object."""
        return OAuth2Authenticator(
            self, self._tap.config_file, "https://api.hubapi.com/oauth/v1/token"
        )

    @property
    def http_headers(self) -> dict:
        """Return the http headers needed."""
        headers = {}
        headers["Content-Type"] = "application/json"
        if "user_agent" in self.config:
            headers["User-Agent"] = self.config.get("user_agent")
        return headers

    @cached_property
    def datetime_fields(self):
        datetime_fields = []
        for key, value in self.schema["properties"].items():
            if value.get("format") == "date-time":
                datetime_fields.append(key)
        return datetime_fields

    @cached_property
    def selected_properties(self):
        selected_properties = []
        for key, value in self.metadata.items():
            if isinstance(key, tuple) and len(key) == 2 and value.selected:
                selected_properties.append(key[-1])
        return selected_properties

    def validate_response(self, response: requests.Response) -> None:
        """Validate HTTP response."""
        if 500 <= response.status_code < 600 or response.status_code in [429, 401]:
            msg = (
                f"{response.status_code} Server Error: "
                f"{response.reason} for path: {self.path}"
            )
            raise RetriableAPIError(msg)

        elif 400 <= response.status_code < 500:
            msg = (
                f"{response.status_code} Client Error: "
                f"{response.reason} for path: {self.path}"
            )
            raise FatalAPIError(msg)

    @staticmethod
    def extract_type(field):
        field_type = field.get("type")
        if field_type in ["string", "enumeration", "phone_number", "date", "json"]:
            return th.StringType
        if field_type == "number":
            return th.StringType
        if field_type == "datetime":
            return th.DateTimeType
        if field_type == "bool":
            return th.BooleanType
        else:
            return None

    def request_schema(self, url, headers):
        response = requests.get(url, headers=headers)
        self.validate_response(response)
        return response

    @cached_property
    def schema(self):
        properties = self.base_properties
        headers = self.http_headers
        headers.update(self.authenticator.auth_headers or {})
        url = self.url_base + self.properties_url
        response = self.request_decorator(self.request_schema)(url, headers=headers)

        fields = response.json()
        for field in fields:
            if not field.get("deleted"):
                property = th.Property(field.get("name"), self.extract_type(field))
                properties.append(property)

        return th.PropertiesList(*properties).to_dict()


class hubspotStreamSchema(hubspotStream):
    
    def get_next_page_token(
        self, response: requests.Response, previous_token: Optional[Any]
    ) -> Optional[Any]:
        """Return a token for identifying next page or None if no more pages."""
        response_json = response.json()
        if response_json.get("has-more"):
            offset = response_json.get("offset")
            vid_offset = response_json.get("vid-offset")
            if offset:
                return dict(offset=offset)
            elif vid_offset:
                return dict(vidOffset=vid_offset)
        return None

    def get_url_params(
        self, context: Optional[dict], next_page_token: Optional[Any]
    ) -> Dict[str, Any]:
        """Return a dictionary of values to be used in URL parameterization."""
        params: dict = {}
        params["count"] = self.page_size
        if next_page_token:
            params.update(next_page_token)
        return params

    def backoff_wait_generator(self):
        """The wait generator used by the backoff decorator on request failure. """
        return backoff.expo(factor=3)

    def backoff_max_tries(self) -> int:
        """The number of attempts before giving up when retrying requests."""
        return 8