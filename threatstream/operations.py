"""
Copyright start
MIT License
Copyright (c) 2024 Fortinet Inc Copyright end
"""

from time import sleep
import validators, json
import os
from os.path import join, exists
from requests import request, exceptions as req_exceptions
from datetime import datetime, timedelta
from connectors.core.connector import Connector, get_logger, ConnectorError
from integrations.crudhub import make_request
from django.conf import settings

logger = get_logger("anomali-threatstream")

FILE_REF = "Attachment ID"
IMPORT_OBSERVABLES = "/api/v2/intelligence/import/"
MAX_RETRY = 5
DELAY_TIME = 10
MAX_REQUEST_TIMEOUT = 600
MACRO_LIST = [
    "IP_Enrichment_Playbooks_IRIs",
    "URL_Enrichment_Playbooks_IRIs",
    "Domain_Enrichment_Playbooks_IRIs",
    "Email_Enrichment_Playbooks_IRIs",
    "FileHash_Enrichment_Playbooks_IRIs",
]

PRIORITY_MAPPING = {
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "Very High": "veryhigh",
    "Very Low": "verylow"
}

CONNECTOR_NAME = "threatstream"

action_list = ["list_incidents", "fetch_incidents"]

whois_action = ["whois_domain", "whois_ip", "get_status", "check_health", "approve_import_job", "reject_import_job"]

resp_list = [
    "get_import_job_status",
    "get_incident",
    "fetch_incidents",
    "get_status",
    "check_health",
    "get_import_jobs",
    "approve_import_job",
    "reject_import_job",
    "delete_incident",
    "list_incidents"
]

query_actions = [
    "get_import_job_status",
    "filter_language_query",
    "delete_incident",
    "get_incident",
    "advance_query",
    "get_import_jobs",
    "list_threat_model_entity",
    "list_observables_associated_threat_bulletin",
    "get_submit_url_status",
    "get_submitted_url_report",
    "intelligence_enrichments",
]

tb_action = [
    "list_threat_bulletins",
    "submit_urls_files",
    "list_incidents_by_indicator",
    "fetch_all_incidents",
]

investigation_actions = [
    "list_investigations",
    "list_investigation_elements"
]
itype_dict = {
    "domain_reputation": "domain",
    "email_reputation": "email",
    "ip_reputation": "ip",
    "url_reputation": "url",
    "file_reputation": "md5",
}

PUBLISHED_STATUS_MAPPING = {
    "Pending Review": "pending_review",
    "Review Requested": "review_requested",
    "Reviewed": "reviewed",
    "Published": "published"
}


def _json_fallback(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    else:
        return obj


def check_server_url(url):
    if not url.startswith("https://"):
        url = "https://" + url
    return url


def get_curr_oper_info(info_json, action):
    try:
        operations = info_json.get("operations")
        exec_action = [
            action_info
            for action_info in operations
            if action_info["operation"] == action
        ]
        return exec_action[0]

    except Exception as err:
        logger.error("{0}".format(str(err)))

        raise ConnectorError("{0}".format(str(err)))


def generate_payload_filter(config, param, itype):
    """Create dict with username and password URL parameters"""
    validation = param.get("validation")

    filter_options = {
        "Exact": "value",
        "Startswith": "value__startswith",
        "Contains": "value__contains",
        "Regex": "value__regex",
        "Regexp": "value__regexp",
    }

    filter_opt = filter_options.get(param.get("filter_option"))

    if param.get("filter_option") == "Exact" and validation:
        logger.info("As validation is True. Validating the Input")
        validate_input(itype, param.get("value"))
    else:
        logger.info("As validation is False. Not Validating the Input")

    payload = dict()
    payload["username"] = config.get("api_username")
    payload["api_key"] = config.get("api_key")
    payload["type"] = itype
    payload[filter_opt] = param.get("value")
    payload["update_id__gt"] = 0
    payload["order_by"] = "update_id"
    return payload


def generate_payload(config, params):
    payload = dict()
    if params:
        payload = {k: v for k, v in params.items() if v is not None and v != ""}

    payload["username"] = config.get("api_username")
    payload["api_key"] = config.get("api_key")
    return payload


def get_all_record(resp_json, params, config):
    if "meta" in resp_json: 
        if resp_json["meta"]["total_count"] != 0:
            if "record_number" in params:
                if params.get("record_number") == "Fetch All Records":
                    make_rest_call(resp_json["meta"]["next"], config, resp_json)

            return resp_json

        else:
            return {
                "message": "Executed successfully returned no data",
                "total_count": resp_json["meta"]["total_count"],
                "result": resp_json,
            }
    else:
        if resp_json is not None:
            return resp_json


def parse_response(resp_json, params, operation_details, config):
    try:
        meta = resp_json.get("meta")

        if meta and meta["next"] is None:  # When there is no more records
            return resp_json

        elif (
            operation_details["operation"] in whois_action
        ):  # When the operation is whois
            if resp_json["data"]:
                return resp_json["data"]

        else:  # when the result has more records.
            return get_all_record(resp_json, params, config)

    except Exception as err:
        raise ConnectorError(err)


def make_rest_call(endpoint, config, result):
    server_url = config.get("base_url")
    if not server_url.startswith("https://"):
        server_url = "https://" + server_url

    endpoint_url = server_url + endpoint
    try:
        response = request(
            "GET",
            endpoint_url,
            params=generate_payload(config, None),
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 200:
            resp_json = response.json()

            result["objects"] = result["objects"] + resp_json.get("objects", None)
            result["meta"] = resp_json.get("meta", None)
            if resp_json and resp_json["meta"]["next"]:
                make_rest_call(resp_json["meta"]["next"], config, result)
        else:
            logger.error(
                "Failure: make_rest_call: Status: {0} {1}".format(
                    str(response.status_code), str(response.text)
                )
            )
            raise ConnectorError(
                "Status: {0} {1}".format(str(response.status_code), str(response.text))
            )

    except Exception as err:
        logger.error("Failure: make_rest_call: {0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def from_cyops_download_file(iri):
    try:
        from integrations.crudhub import download_file_from_cyops
    except:
        from connectors.cyops_utilities.builtins import download_file_from_cyops

    file_name = None
    attachment_data = make_request(iri, "GET")
    if iri.startswith("/api/3/attachments/"):
        file_iri = attachment_data["file"]["@id"]
        file_name = attachment_data["file"]["filename"]
        logger.info("file id = {0}, file_name = {1}".format(file_iri, file_name))
    else:
        file_iri = iri
    dw_file_md = download_file_from_cyops(file_iri)
    file_path = join("/tmp", dw_file_md["cyops_file_path"])
    if file_name == None:
        file_name = (
            dw_file_md["filename"]
            if dw_file_md["filename"] != None
            else "Upload_from_the_FortiSOAR"
        )
    return file_path, file_name


def handle_date(days):
    if days:
        datetime_now = datetime.now()
        expiration_date = datetime_now + timedelta(days)
        return expiration_date.strftime("%Y-%m-%d %H:%M:%S")
    return days


def validate_input(itype, value):
    validator = {
        "domain": validators.domain,
        "email": validators.email,
        "ip": validators.ipv4,
        "url": validators.url,
        "md5": validators.md5,
    }
    action = validator.get(itype)
    if action and not action(str(value)):
        raise ConnectorError("Invalid {0} {1}".format(itype, value))
    else:
        return True


def add_attachment_to_tb(tb_id, reference_id, config):
    try:
        server_url = check_server_url(config.get("base_url"))
        payload = generate_payload(config, None)
        file_path, file_name = from_cyops_download_file(reference_id)
        logger.info("Filename : {0} Filepath: {1}".format(file_name, file_path))
        files = {
            "attachment": (file_name, open(file_path, "rb")),
            "filename": (None, file_name),
        }
        endpoint_file = server_url + "/api/v1/tipreport/{0}/attachment/".format(tb_id)

        response = request(
            "POST",
            endpoint_file,
            params=payload,
            files=files,
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 201:
            return response.json()
        else:
            logger.error(
                "Attachment Creation Failed {0}: {1}".format(
                    response.status_code, response.reason
                )
            )
            raise ConnectorError(
                "Attachment Creation Failed {0}: {1}".format(
                    response.status_code, response.reason
                )
            )

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def import_observables(config, params):
    file_path = None
    server_url = check_server_url(config.get("base_url"))
    try:
        payload = generate_payload(config, None)
        sev_dict = {
            "Low": "low",
            "Medium": "medium",
            "High": "high",
            "Very High": "very-high",
        }
        severity = sev_dict.get(params.get("severity"), "low")
        classification_dict = {"Public": "public", "Private": "private"}
        classification = classification_dict.get(
            params.get("classification"), "private"
        )

        expiration_day = {
            "90 days": 90,
            "60 days": 60,
            "30 days": 30,
            "Never": "",
            "Custom": params.get("custom_days"),
        }
        expiration_ts = handle_date(
            expiration_day.get(params.get("expiration_ts"), "90 days")
        )

        data = {
            "confidence": (None, params.get("confidence")),
            "severity": (None, severity),
            "classification": (None, classification),
            "expiration_ts": (None, expiration_ts),
            "ip_mapping": (None, params.get("ip_mapping")),
            "domain_mapping": (None, params.get("domain_mapping")),
            "url_mapping": (None, params.get("url_mapping")),
            "email_mapping": (None, params.get("email_mapping")),
            "md5_mapping": (None, params.get("md5_mapping")),
            "threat_type": params.get('threat_type') if params.get('threat_type') else "malware",
            "default_state": "active" if params.get('require_approval') is False else None,
            "reject_benign": params.get('reject_benign', True)
        }
        tags = params.get("notes")
        if isinstance(tags, list):
            tags = ", ".join(tags)
        if tags:
            data.setdefault("notes", (None, tags))

        trusted_circles = params.get("trusted_circles")
        if trusted_circles:
            data.setdefault("trusted_circles", (None, trusted_circles))

        observables_data = params.get("data")
        if observables_data:
            data.setdefault("datatext", (None, observables_data))

        source_confidence_weight = params.get("source_confidence_weight")
        if source_confidence_weight:
            data.setdefault(
                "source_confidence_weight", (None, source_confidence_weight)
            )

        reference_id = str(params.get("reference_id"))

        if not (reference_id or observables_data):
            logger.error("Either File Details or Observable data are required")
            raise ConnectorError("Either File Details or Observable data are required")

        data = {k: v for k, v in data.items() if v not in [None, "", (None, None), (None, "")]}
        files = None
        if reference_id:
            file_path, file_name = from_cyops_download_file(reference_id)
            files = {
                "file": (file_name, open(file_path, "r").read(), "text/csv")
            }

        endpoint = server_url + IMPORT_OBSERVABLES

        response = request(
            "POST",
            endpoint,
            params=payload,
            files=files,
            data=data,
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )

        if file_path and exists(file_path):
            os.remove(file_path)

        if response.ok:
            return response.json()
        else:
            try:
                logger.error("Failure {0}: {1}".format(response.status_code, response.json()))
                raise ConnectorError("Failure {0}: {1}".format(response.status_code, response.json()))
            except (KeyError, json.decoder.JSONDecodeError) as error:
                logger.error("Failed {0}: {1}".format(response.status_code, response.reason))
                raise ConnectorError("Failure {0}: {1}".format(response.status_code, response.reason))
    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def create_incident(config, params):
    try:
        server_url = check_server_url(config.get("base_url"))
        payload = generate_payload(config, None)
        query_data = {
            "name": params.get("name"),
            "is_public": params.get("is_public"),
            "status": 2,
        }

        tags = params.get("tags")
        if tags:
            if isinstance(tags, str):
                tags = tags.split(",")

        query_data["tags"] = tags

        tlp = params.get("tlp")
        if tlp:
            query_data["tlp"] = tlp.lower()

        intelligence = params.get("intelligence")
        if intelligence:
            if isinstance(intelligence, int):
                intel_list = list()
                query_data["intelligence"] = intel_list.append(intelligence)
            else:
                query_data["intelligence"] = intelligence

        fields = params.get("fields")
        if fields:
            if type(fields) is dict:
                query_data.update(fields)

        endpoint = server_url + "/api/v1/incident/"

        header = {"Content-Type": "application/json"}

        response = request(
            "POST",
            endpoint,
            headers=header,
            params=payload,
            data=json.dumps(query_data),
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 201:
            return response.json()
        else:
            logger.error(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
            raise ConnectorError(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def update_incident(config, params):
    try:
        server_url = check_server_url(config.get("base_url"))
        payload = generate_payload(config, None)
        result = {k: v for k, v in params.items() if v is not None and v != ""}
        params["operation"] = "update_incident"
        stat_dict = get_status(config, params)
        result["status"] = stat_dict.get(result.get("status"))

        if "fields" in result:
            extra_fields = result.pop("fields")
            if type(extra_fields) is dict:
                result.update(extra_fields)

        endpoint = server_url + "/api/v1/incident/{0}/".format(params.get("value"))

        header = {"Content-Type": "application/json"}
        result.pop("value")
        response = request(
            "PATCH",
            endpoint,
            headers=header,
            params=payload,
            data=json.dumps(result),
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 202:
            return response.json()
        else:
            logger.error(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
            raise ConnectorError(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def list_incidents(config, params):
    """
    list_incidents action has been deprecated from connector version 2.2.0
    To avoid PB failure kept this code and made action "enabled": false
    """
    try:
        operation_details = dict()
        if params.get("value", None):
            operation_details["http_method"] = "GET"
            operation_details[
                "endpoint"
            ] = "/api/v1/incident/associated_with_intelligence/?value={value}"
            operation_details["operation"] = "list_incidents"
            resp = api_request(config, params, operation_details)
            return resp

        else:
            operation_details["http_method"] = "GET"
            operation_details["endpoint"] = "/api/v1/incident/"
            operation_details["operation"] = "list_incidents"
            resp = api_request(config, params, operation_details)
            return resp

    except Exception as err:
        logger.error("Failure {0}".format(str(err)))
        raise ConnectorError("Failure {0}".format(str(err)))


def fetch_incidents(config, params):
    operation_details = dict()
    if params.get("value", None):
        operation_details["http_method"] = "GET"
        operation_details["endpoint"] = "/api/v1/incident/?{value}"
        operation_details["operation"] = "fetch_incidents"
        resp = api_request(config, params, operation_details)
        return resp

    else:
        operation_details["http_method"] = "GET"
        operation_details["endpoint"] = "/api/v1/incident/"
        operation_details["operation"] = "fetch_all_incidents"
        resp = api_request(config, params, operation_details)
        return resp


def api_request(config, params, operation_details):
    try:
        server_url = check_server_url(config.get("base_url"))

        if operation_details["operation"] in query_actions:
            payload = generate_payload(config, None)
            param_value = params.get("value")
            endpoint = server_url + operation_details["endpoint"].format(
                value=param_value
            )

            if "record_number" in params:
                if params.get("record_number") == "Fetch Limited Records":
                    payload["limit"] = params.get("limit")
                    payload["offset"] = params.get("offset", 0)
                else:
                    payload["limit"] = 0
                    payload["offset"] = 0

        elif operation_details["operation"] in whois_action:
            payload = generate_payload(config, None)
            param_value = params.get("value")
            endpoint = server_url + operation_details["endpoint"].format(
                value=param_value
            )

        elif operation_details["operation"] in action_list:
            payload = generate_payload(config, params)
            param_value = params.get("value")
            payload.pop("value")
            if "limit" not in payload:
                payload.setdefault("limit", 0)

            if "offset" not in payload:
                payload.setdefault("offset", 0)

            endpoint = server_url + operation_details["endpoint"].format(
                value=param_value
            )

        elif operation_details["operation"] in tb_action:
            endpoint = config.get("base_url") + operation_details["endpoint"]
            payload = generate_payload(config, params)
            if "record_number" in params:
                if params.get("record_number") == "Fetch Limited Records":
                    payload["limit"] = params.get("limit")
                    payload["offset"] = params.get("offset", 0)
                else:
                    payload["limit"] = 1000
                    payload["offset"] = 0
                payload.pop("record_number")

        elif operation_details["operation"] in investigation_actions:
            base_url = check_server_url(config.get("base_url"))
            endpoint = base_url + operation_details["endpoint"]
            if operation_details["operation"] == 'list_investigations':
                investigation_id = params.pop("investigation_id")
                if investigation_id:
                    endpoint = f"{endpoint}{investigation_id}/"
            for key in ['priority', 'status']:
                if params.get(key):
                    params[key] = params.get(key).lower()
            if params.get('add_related_indicators'):
                params['add_related_indicators'] = 1 if params.get('add_related_indicators') == 'Yes' else 0
            payload = generate_payload(config, params)
            
            if "record_number" in params:
                if params.get("record_number") == "Fetch Limited Records":
                    payload["limit"] = params.get("limit")
                    payload["offset"] = params.get("offset", 0)
                else:
                    payload["limit"] = 1000
                    payload["offset"] = 0
                payload.pop("record_number")

        else:
            payload = generate_payload_filter(
                config, params, itype_dict.get(operation_details["operation"])
            )
            endpoint = "{0}{1}".format(server_url, operation_details["endpoint"])

            if "record_number" in params:
                if params.get("record_number") == "Fetch Limited Records":
                    payload["limit"] = params.get("limit")
                    payload["offset"] = params.get("offset", 0)
                else:
                    payload["limit"] = 0
                    payload["offset"] = 0

        # Common REST request query handler.

        retry_count = 0
        while retry_count < MAX_RETRY:
            try:
                response = request(
                    operation_details["http_method"],
                    endpoint,
                    params=payload,
                    verify=config.get("verify_ssl"),
                    timeout=MAX_REQUEST_TIMEOUT
                )
                if response.status_code in (200, 202):
                    if operation_details["operation"] in list(
                        set(resp_list) | set(query_actions)
                    ):
                        resp_json = response.json()
                        if params.get("record_number") == "Fetch All Records":
                            if not resp_json["meta"]["next"] is None:
                                return get_all_record(resp_json, params, config)
                        return resp_json
                    else:
                        resp_json = response.json()
                        return parse_response(resp_json, params, operation_details, config)

                elif response.status_code == 204:
                    return {
                        "result": "Successfully deleted the incident with ID {0}".format(
                            params.get("value")
                        )
                    }

                raise ConnectorError(
                    "{0}:{1} {2}".format(
                        response.status_code,
                        response.reason,
                        response.text if not response.text.startswith("<!DOCTYPE") else "",
                    )
                )
            except (req_exceptions.ChunkedEncodingError, req_exceptions.ConnectionError,
                    req_exceptions.ReadTimeout, req_exceptions.ProxyError, ConnectionResetError) as ex:
                retry_count += 1

                if retry_count >= MAX_RETRY:
                    logger.error("Retry limit reached: {}".format(retry_count))
                    raise Exception(ex)
                else:
                    logger.error("Retries attempted: {}".format(retry_count))
                    sleep(DELAY_TIME)
    except req_exceptions.SSLError:
        logger.error("An SSL error occurred")
        raise ConnectorError("An SSL error occurred")
    except req_exceptions.ConnectionError:
        logger.error("A connection error occurred")
        raise ConnectorError("A connection error occurred")
    except req_exceptions.Timeout:
        logger.error("The request timed out")
        raise ConnectorError("The request timed out")
    except req_exceptions.RequestException:
        logger.error("There was an error while handling the request")
        raise ConnectorError("There was an error while handling the request")
    except Exception as e:
        logger.error(e)
        raise ConnectorError(e)


def check_health(config):
    try:
        operation_details = dict()
        operation_details["http_method"] = "GET"
        operation_details["endpoint"] = "/api/v2/intelligence"
        operation_details["operation"] = "check_health"
        response = api_request(config, params={}, operation_details=operation_details)

        if response:
            return True
        else:
            raise ConnectorError(
                "Failed to connect to Anomali ThreatStream"
                "Status code: {0}".format(str(response.status_code))
            )
    except Exception as err:
        logger.error("Failure {0}".format(str(err)))
        raise ConnectorError(str(err))


def get_status(config, params):
    try:
        if params.get("operation") == "update_incident":
            build_dict = True
        else:
            build_dict = False
        operation_details = dict()
        operation_details["http_method"] = "GET"
        operation_details["endpoint"] = "/api/v1/incidentstatustype/"
        operation_details["operation"] = "get_status"
        response = api_request(config, params, operation_details)
        status_list = response["objects"]
        response_list = []
        response_dict = {}
        for i in range(len(status_list)):
            response_dict[status_list[i].get("display_name")] = (
                status_list[i].get("id")
                if build_dict
                else response_list.append(status_list[i].get("display_name"))
            )

        return response_dict if build_dict else response_list

    except Exception as err:
        logger.error("Failure {0}".format(str(err)))
        raise ConnectorError(str(err))


def list_threat_bulletins(config, params):
    try:
        operation_details = dict()
        params["skip_associations"] = True
        params["skip_intelligence"] = True
        operation_details["http_method"] = "GET"
        operation_details["operation"] = "list_threat_bulletins"

        if params.get("query"):
            operation_details["endpoint"] = "/api/v1/tipreport/?{0}".format(
                params.get("query")
            )
            params.pop("query")
        else:
            operation_details["endpoint"] = "/api/v1/tipreport/"

        resp = api_request(config, params, operation_details)

        return resp
    except Exception as err:
        logger.error("Failure {0}".format(str(err)))
        raise ConnectorError(str(err))


def list_threat_model_entity(config, params):
    try:
        operation_details = dict()

        operation_details["http_method"] = "GET"
        operation_details["operation"] = "list_threat_model_entity"
        operation_details["endpoint"] = "/api/v1/tipreport/{0}/{1}".format(
            params.get("id"), params.get("entity_type").lower()
        )
        resp = api_request(config, params, operation_details)
        return resp
    except Exception as err:
        logger.error("Failure {0}".format(str(err)))
        raise ConnectorError(str(err))


def create_threat_bulletin(config, params):
    try:
        server_url = check_server_url(config.get("base_url"))
        payload = generate_payload(config, None)
        query_data = {
            "name": params.get("name"),
            "is_public": params.get("is_public"),
            "body_content_type": params.get("body_content_type", '').lower(),
            "status": "new",
        }

        description = params.get("body")
        if description:
            query_data["body"] = description

        tlp = params.get("tlp")
        if tlp:
            query_data["tlp"] = tlp.lower()

        fields = params.get("fields")
        if fields:
            if type(fields) is dict:
                query_data.update(fields)

        endpoint = server_url + "/api/v1/tipreport/"

        header = {"Content-Type": "application/json"}

        response = request(
            "POST",
            endpoint,
            headers=header,
            params=payload,
            data=json.dumps(query_data),
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 201:
            reference_id = params.get("reference_id")
            if not reference_id:
                return response.json()
            else:
                tb_id = response.json().get("id")
                resp = add_attachment_to_tb(tb_id, reference_id, config)
                return {"attachment": resp, "threat_bulletin": response.json()}

        else:
            logger.error(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
            raise ConnectorError(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def update_threat_bulletin(config, params):
    try:
        server_url = check_server_url(config.get("base_url"))
        payload = generate_payload(config, None)
        result = {k: v for k, v in params.items() if v is not None and v != ""}
        if result.get("status"):
            result["status"] = PUBLISHED_STATUS_MAPPING.get(result["status"], result["status"])
        tb_id = params.get("tb_id")
        result.pop("tb_id")

        if "fields" in result:
            extra_fields = result.pop("fields")
            if type(extra_fields) is dict:
                result.update(extra_fields)

        endpoint = server_url + "/api/v1/tipreport/{0}/".format(tb_id)

        header = {"Content-Type": "application/json"}
        response = request(
            "PATCH",
            endpoint,
            headers=header,
            params=payload,
            data=json.dumps(result),
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 202:
            reference_id = params.get("reference_id")
            if not reference_id:
                return response.json()
            else:
                resp = add_attachment_to_tb(tb_id, reference_id, config)
                return {"attachment": resp, "threat_bulletin": response.json()}
        else:
            logger.error(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
            raise ConnectorError(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def submit_urls_files(config, params):
    try:
        server_url = check_server_url(config.get("base_url"))
        endpoint = server_url + "/api/v1/submit/new/"
        payload = generate_payload(config, None)
        files = {
            "report_radio-classification": (None, params.get("classification", '').lower()),
            "report_radio-platform": (None, params.get("platform")),
            "detail": (None, params.get("detail")),
            "use_premium_sandbox": (None, params.get("use_premium_sandbox", False)),
        }
        radio_url = params.get("radio_url")
        if radio_url:
            files["report_radio-url"] = (None, radio_url)

        reference_id = params.get("reference_id")
        if reference_id:
            file_path, file_name = from_cyops_download_file(reference_id)
            logger.info("Filename : {0} Filepath: {1}".format(file_name, file_path))
            files.setdefault("report_radio-file", (file_name, open(file_path, "rb")))

        trusted_circles = params.get("trusted_circles")
        if trusted_circles:
            files["trusted_circles"] = (None, trusted_circles)

        response = request(
            "POST",
            endpoint,
            params=payload,
            files=files,
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.status_code == 202:
            return response.json()
        else:
            logger.error(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
            raise ConnectorError(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def intelligence_enrichments(config, params):
    try:
        operation_details = dict()
        services = params.get("services")
        itype = params.get("itype", '')

        if services == "Passive DNS":
            operation_details["http_method"] = "GET"
            operation_details["operation"] = "intelligence_enrichments"
            operation_details["endpoint"] = (
                "/api/v1/pdns/{0}/".format(itype.lower()) + "{value}/"
            )

        elif services == "Recorded Future":
            operation_details["http_method"] = "GET"
            operation_details["operation"] = "intelligence_enrichments"
            operation_details["endpoint"] = (
                "/api/v1/recorded_future/search/{0}/".format(itype.lower()) + "{value}/"
            )

        elif services == "Risk IQ":
            operation_details["http_method"] = "GET"
            operation_details["operation"] = "intelligence_enrichments"
            operation_details["endpoint"] = "/api/v1/riskiq_ssl/certificate/{value}"

        resp = api_request(config, params, operation_details)
        return resp

    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


def create_or_update_investigation(config, params):
    try:
        server_url = check_server_url(config.get("base_url"))
        investigation_id = params.pop("investigation_id", '')
        if investigation_id or investigation_id == 0:
            endpoint = f"{server_url}/api/v1/investigation/{investigation_id}/"
            method = "PATCH"
        else:
            endpoint = f"{server_url}/api/v1/investigation/"
            method = "POST"
        additional_attributes = params.pop('additional_attributes', {})
        params['priority'] = PRIORITY_MAPPING.get(params.get('priority'), params.get('priority'))
        for k in ['tlp', 'status']:
            params[k] = params.get(k, '').lower()
        payload = {k: v for k, v in params.items() if v != '' and v is not None}
        if additional_attributes:
            payload.update(additional_attributes)
        header = {"Content-Type": "application/json"}
        response = request(
            method,
            endpoint,
            headers=header,
            params=generate_payload(config, None),
            json=payload,
            verify=config.get("verify_ssl"),
            timeout=MAX_REQUEST_TIMEOUT
        )
        if response.ok:
            return response.json()
        else:
            logger.error(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
            raise ConnectorError(
                "Failure {0}: {1}".format(response.status_code, response.reason)
            )
    except Exception as err:
        logger.error("{0}".format(str(err)))
        raise ConnectorError("{0}".format(str(err)))


operation_sym = {
    "create_incident": create_incident,
    "update_incident": update_incident,
    "submit_observables": import_observables,
    "fetch_incidents": fetch_incidents,
    "list_incidents": list_incidents,
    "get_status": get_status,
    "list_threat_bulletins": list_threat_bulletins,
    "create_threat_bulletin": create_threat_bulletin,
    "list_threat_model_entity": list_threat_model_entity,
    "update_threat_bulletin": update_threat_bulletin,
    "submit_urls_files": submit_urls_files,
    "intelligence_enrichments": intelligence_enrichments,
    "update_investigation": create_or_update_investigation,
    "create_investigation": create_or_update_investigation,
}