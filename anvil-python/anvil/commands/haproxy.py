# Copyright (c) 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ipaddress
import logging
from os import environ, mkdir
import os.path
from typing import Any, ClassVar, List

from rich.console import Console
from sunbeam.clusterd.client import Client
from sunbeam.commands.terraform import TerraformInitStep
from sunbeam.jobs import questions
from sunbeam.jobs.common import BaseStep
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.steps import (
    AddMachineUnitsStep,
    DeployMachineApplicationStep,
)

from anvil.jobs.manifest import Manifest
from anvil.jobs.steps import RemoveMachineUnitStep

LOG = logging.getLogger(__name__)

APPLICATION = "haproxy"
CONFIG_KEY = "TerraformVarsHaproxyPlan"
HAPROXY_CONFIG_KEY = "TerraformVarsHaproxy"
HAPROXY_APP_TIMEOUT = 180  # 3 minutes, managing the application should be fast
HAPROXY_UNIT_TIMEOUT = (
    1200  # 15 minutes, adding / removing units can take a long time
)
HAPROXY_CERTS_DIR = os.path.join(environ["HOME"], "certs/")
LOG = logging.getLogger(__name__)


def validate_cert_file(filepath: str | None) -> None:
    if filepath is None:
        return
    if not os.path.isfile(filepath):
        raise ValueError(f"{filepath} does not exist")
    with open(filepath) as f:
        if "BEGIN CERTIFICATE" not in f.read():
            raise ValueError("Invalid certificate file")


def validate_key_file(filepath: str | None) -> None:
    if filepath is None:
        return
    if not os.path.isfile(filepath):
        raise ValueError(f"{filepath} does not exist")
    with open(filepath) as f:
        if "BEGIN PRIVATE KEY" not in f.read():
            raise ValueError("Invalid key file")


def validate_virtual_ip(value: str) -> str:
    """We allow passing an empty IP for virtual_ip"""
    if value == "":
        return ""
    try:
        return ipaddress.ip_address(value).exploded
    except ValueError as e:
        raise ValueError(f"{value} is not a valid IP address: {e}")


def haproxy_questions() -> dict[str, questions.PromptQuestion]:
    return {
        "virtual_ip": questions.PromptQuestion(
            "Virtual IP to use for the Cluster in HA",
            default_value="",
            validation_function=validate_virtual_ip,
        ),
        "ssl_cert": questions.PromptQuestion(
            "Path to SSL Certificate for HAProxy (enter nothing to skip TLS)",
            default_value=None,
            validation_function=validate_cert_file,
        ),
        "ssl_key": questions.PromptQuestion(
            "Path to private key for the SSL certificate (enter nothing to skip TLS)",
            default_value=None,
            validation_function=validate_key_file,
        ),
    }


class DeployHAProxyApplicationStep(DeployMachineApplicationStep):
    """Deploy HAProxy application using Terraform"""

    _HAPROXY_CONFIG = HAPROXY_CONFIG_KEY
    _TLS_SERVICES_CONFIG: str = f"""- service_name: haproxy_service
  service_host: "0.0.0.0"
  service_port: 443
  service_options:
    - balance leastconn
    - cookie SRVNAME insert
  server_options: maxconn 100 cookie S{"{i}"} check
  crts: [{HAPROXY_CERTS_DIR}]
"""
    _DEFAULT_SERVICES_CONFIG: str = """- service_name: haproxy_service
  service_host: "0.0.0.0"
  service_port: 80
  service_options: [balance leastconn, cookie SRVNAME insert]
  server_options: maxconn 100 cookie S{i} check
"""

    def __init__(
        self,
        client: Client,
        manifest: Manifest,
        jhelper: JujuHelper,
        model: str,
        deployment_preseed: dict[Any, Any] | None = None,
        accept_defaults: bool = False,
        refresh: bool = False,
    ):
        super().__init__(
            client,
            manifest,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "haproxy-plan",
            "Deploy HAProxy",
            "Deploying HAProxy",
            refresh,
        )
        self.preseed = deployment_preseed or {}
        self.accept_defaults = accept_defaults
        self.use_tls_termination = False

    def get_application_timeout(self) -> int:
        return HAPROXY_APP_TIMEOUT

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user.

        :return: True if the step can ask the user for prompts,
                 False otherwise
        """
        # No need to prompt for questions in case of refresh
        if self.refresh:
            return False

        return True

    def prompt(self, console: Console | None = None) -> None:
        variables = questions.load_answers(self.client, self._HAPROXY_CONFIG)
        variables.setdefault("virtual_ip", "")
        variables.setdefault("ssl_cert", None)
        variables.setdefault("ssl_key", None)

        # Set defaults
        self.preseed.setdefault("virtual_ip", "")
        self.preseed.setdefault("ssl_cert", None)
        self.preseed.setdefault("ssl_key", None)

        haproxy_config_bank = questions.QuestionBank(
            questions=haproxy_questions(),
            console=console,
            preseed=self.preseed.get("haproxy"),
            previous_answers=variables,
            accept_defaults=self.accept_defaults,
        )

        variables["virtual_ip"] = haproxy_config_bank.virtual_ip.ask()

        variables["ssl_cert"] = cert_filepath = haproxy_config_bank.ssl_cert.ask()
        variables["ssl_key"] = key_filepath = haproxy_config_bank.ssl_key.ask()
        if cert_filepath is not None and key_filepath is not None:
            with open(cert_filepath) as cert_file:
                cert = cert_file.read()
            with open(key_filepath) as key_file:
                key = key_file.read()
            if not os.path.isdir(HAPROXY_CERTS_DIR):
                mkdir(HAPROXY_CERTS_DIR)
            with open(
                os.path.join(HAPROXY_CERTS_DIR, "haproxy.pem"), "w"
            ) as combined_file:
                combined_file.write(key + cert)
            self.use_tls_termination = True
        else:
            LOG.debug(
                "No certificate/key provided, skipping TLS configuration"
            )

        LOG.debug(variables)
        questions.write_answers(self.client, self._HAPROXY_CONFIG, variables)

    def extra_tfvars(self) -> dict[str, Any]:
        variables: dict[str, Any] = questions.load_answers(
            self.client, self._HAPROXY_CONFIG
        )
        # Terraform does not need the content of these answers
        variables.pop("ssl_cert", None)
        variables.pop("ssl_key", None)

        if self.use_tls_termination:
            variables["haproxy_services_yaml"] = self._TLS_SERVICES_CONFIG
            variables["haproxy_port"] = 443
        else:
            variables["haproxy_port"] = 80
        LOG.debug(f"extra tfvars: {variables}")
        return variables


class AddHAProxyUnitsStep(AddMachineUnitsStep):
    """Add HAProxy Unit."""

    def __init__(
        self,
        client: Client,
        names: list[str] | str,
        jhelper: JujuHelper,
        model: str,
    ):
        super().__init__(
            client,
            names,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Add HAProxy unit",
            "Adding HAProxy unit to machine",
        )

    def get_unit_timeout(self) -> int:
        return HAPROXY_UNIT_TIMEOUT


class RemoveHAProxyUnitStep(RemoveMachineUnitStep):
    """Remove HAProxy Unit."""

    def __init__(
        self, client: Client, name: str, jhelper: JujuHelper, model: str
    ):
        super().__init__(
            client,
            name,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Remove HAProxy unit",
            "Removing HAProxy unit from machine",
        )

    def get_unit_timeout(self) -> int:
        return HAPROXY_UNIT_TIMEOUT


def haproxy_install_steps(
    client: Client,
    manifest: Manifest,
    jhelper: JujuHelper,
    model: str,
    fqdn: str,
    accept_defaults: bool,
    preseed: dict[Any, Any],
) -> List[BaseStep]:
    return [
        TerraformInitStep(manifest.get_tfhelper("haproxy-plan")),
        DeployHAProxyApplicationStep(
            client,
            manifest,
            jhelper,
            model,
            accept_defaults=accept_defaults,
            deployment_preseed=preseed,
        ),
        AddHAProxyUnitsStep(client, fqdn, jhelper, model),
    ]


def haproxy_upgrade_steps(
    client: Client,
    manifest: Manifest,
    jhelper: JujuHelper,
    model: str,
    preseed: dict[Any, Any],
) -> List[BaseStep]:
    return [
        TerraformInitStep(manifest.get_tfhelper("haproxy-plan")),
        DeployHAProxyApplicationStep(
            client,
            manifest,
            jhelper,
            model,
            deployment_preseed=preseed,
            refresh=True,
        ),
    ]
