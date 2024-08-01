"""Integrate with AliDNS."""
from __future__ import annotations
import asyncio
from ipaddress import IPv6Address
import json
import logging

from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime, timedelta
from typing import Any
import ifaddr

from aliyunsdkalidns.request.v20150109.AddDomainRecordRequest import (
    AddDomainRecordRequest,
)
from aliyunsdkalidns.request.v20150109.DescribeSubDomainRecordsRequest import (
    DescribeSubDomainRecordsRequest,
)
from aliyunsdkalidns.request.v20150109.UpdateDomainRecordRequest import (
    UpdateDomainRecordRequest,
)
from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException
from aliyunsdkcore.client import AcsClient
import async_timeout
import voluptuous as vol

from homeassistant.const import CONF_DOMAIN, CONF_SCAN_INTERVAL
import homeassistant.helpers.config_validation as cv
from homeassistant.components import network
from homeassistant.core import (
    CALLBACK_TYPE,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.loader import bind_hass
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)
TIMEOUT = 30  # seconds

DEFAULT_INTERVAL = timedelta(minutes=10)
DOMAIN = "alidns"

CONF_ACCESS_ID = "access_id"
CONF_ACCESS_KEY = "access_key"
CONF_SUB_DOMAIN = "sub_domain"
INTERVAL = timedelta(minutes=5)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_ACCESS_ID): cv.string,
                vol.Required(CONF_ACCESS_KEY): cv.string,
                vol.Required(CONF_DOMAIN): cv.string,
                vol.Required(CONF_SUB_DOMAIN): cv.string,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_INTERVAL): vol.All(
                    cv.time_period, cv.positive_timedelta
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Initialize the AliDNS component."""
    conf = config[DOMAIN]
    access_id = conf[CONF_ACCESS_ID]
    access_key = conf[CONF_ACCESS_KEY]
    domain = conf[CONF_DOMAIN]
    sub_domain = conf[CONF_SUB_DOMAIN]

    loop = asyncio.get_running_loop()

    acs_client = await loop.run_in_executor(None, AcsClient, access_id, access_key)

    result = await _update_alidns(hass, acs_client, domain, sub_domain)

    if result is False:
        return False

    async def update_domain_callback(now):
        """Update the AliDNS entry."""
        await _update_alidns(hass, acs_client, domain, sub_domain)

    intervals = (
        INTERVAL,
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(minutes=30),
    )
    async_track_time_interval_backoff(hass, update_domain_callback, intervals)

    # hass.helpers.event.async_track_time_interval(
    #     update_domain_callback, update_interval
    # )

    return True


async def _update_alidns(hass:HomeAssistant, acs_client, domain, sub_domain):
    """Update AliDNS."""
    _LOGGER.warning(f"start update dns")
    adapters = ifaddr.get_adapters()
    for adapter in adapters:
        for ip in adapter.ips:
            _LOGGER.warning(f"ip from ifaddr {format(ip)}")

    try:
        with async_timeout.timeout(TIMEOUT):
            my_ip = ''
            adapters = ifaddr.get_adapters()
            for adapter in adapters:
                for ip in adapter.ips:
                    if ip.is_IPv6:
                        if ip.ip[0].startswith('240e'):
                            my_ip = ip.ip[0]
                            _LOGGER.warning(f"ip from ifaddr {format(ip)}")
            _LOGGER.warning(f"address {my_ip}")

            dns_record = None
            request = DescribeSubDomainRecordsRequest()
            request.set_accept_format("json")

            request.set_SubDomain(sub_domain + "." + domain)

            loop = asyncio.get_running_loop()
            # response = acs_client.do_action_with_exception(request)
            response = await loop.run_in_executor(None, acs_client.do_action_with_exception, request)


            response_json = json.loads(response)
            if response_json["TotalCount"] > 0:
                    dns_record = response_json["DomainRecords"]["Record"][0]

            if dns_record:
                if my_ip != dns_record["Value"]:
                    _LOGGER.info("Update Domain Record")
                    request = UpdateDomainRecordRequest()
                    request.set_accept_format("json")
                    request.set_RecordId(dns_record["RecordId"])
                    request.set_RR(sub_domain)
                    request.set_Type("AAAA")
                    request.set_Value(my_ip)

                    # acs_client.do_action_with_exception(request)
                    await loop.run_in_executor(None, acs_client.do_action_with_exception, request)
                _LOGGER.info("No need to Update")
            else:
                request = AddDomainRecordRequest()
                request.set_accept_format("json")

                request.set_DomainName(domain)
                request.set_RR(sub_domain)
                request.set_Type("AAAA")
                request.set_Value(my_ip)

                # acs_client.do_action_with_exception(request)
                await loop.run_in_executor(None, acs_client.do_action_with_exception, request)

                _LOGGER.info("Add Domain Record")

            return True

    except ServerException as s_err:
        _LOGGER.warning("Failed to update alidns. Server Exception:%s", s_err)

    except ClientException as c_err:
        _LOGGER.warning("Failed to update alidns. Client Exception:%s", c_err)

    except asyncio.TimeoutError:
        _LOGGER.warning("Timeout to update alidns")

    return False

@callback
@bind_hass
def async_track_time_interval_backoff(
    hass: HomeAssistant,
    action: Callable[[datetime], Coroutine[Any, Any, bool]],
    intervals: Sequence[timedelta],
) -> CALLBACK_TYPE:
    """Add a listener that fires repetitively at every timedelta interval."""
    remove: CALLBACK_TYPE | None = None
    failed = 0

    async def interval_listener(now: datetime) -> None:
        """Handle elapsed intervals with backoff."""
        nonlocal failed, remove
        try:
            failed += 1
            if await action(now):
                failed = 0
        finally:
            delay = intervals[failed] if failed < len(intervals) else intervals[-1]
            remove = async_call_later(
                hass, delay.total_seconds(), interval_listener_job
            )

    interval_listener_job = HassJob(interval_listener, cancel_on_shutdown=True)
    hass.async_run_hass_job(interval_listener_job, dt_util.utcnow())

    def remove_listener() -> None:
        """Remove interval listener."""
        if remove:
            remove()

    return remove_listener