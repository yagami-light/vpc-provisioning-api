"""Create and delete VPC networks with the EC2 API."""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError, ParamValidationError

from .config import get_ec2_client, get_settings
from .errors import ProvisioningError, ProvisioningFailure, classify_ec2_error
from .models import RequestRecord, ResourceSet, SubnetResource, VpcRequest

logger = logging.getLogger(__name__)

TAG_MANAGED_BY = "ManagedBy"
TAG_REQUEST_ID = "prov:requestId"
TAG_LOGICAL_ID = "prov:logicalId"


def first_or_none(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first item, or None when the list is empty."""
    return items[0] if items else None


def as_provisioning_error(exc: Exception, operation_name: str) -> ProvisioningError:
    """Turn a botocore failure into one of our own provisioning errors."""
    if isinstance(exc, ParamValidationError):
        return ProvisioningFailure(
            f"ec2:{operation_name} was called with invalid parameters: {exc}",
            code="invalid_request",
        )
    error = exc.response.get("Error", {})
    code = error.get("Code", "") or "UnknownError"
    message = error.get("Message", "no message provided")
    logger.warning("ec2 call failed operation=%s errorCode=%s", operation_name, code)
    return classify_ec2_error(code, f"ec2:{operation_name} failed ({code}): {message}")


class NetworkProvisioner:
    """Creates and deletes the network described by one request."""

    def __init__(self, ec2_client: Any | None = None) -> None:
        self._ec2 = ec2_client or get_ec2_client()

    # ==================================================================
    # EC2 calls.  One method per operation, and each one calls the boto3
    # method by its real name, so nothing has to be looked up anywhere.
    # Any failure becomes a ProvisioningError.
    # ==================================================================

    def find_vpcs(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Describe the VPCs that match `filters`."""
        try:
            return self._ec2.describe_vpcs(Filters=filters)["Vpcs"]
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "describe_vpcs") from exc

    def find_subnets(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Describe the subnets that match `filters`."""
        try:
            return self._ec2.describe_subnets(Filters=filters)["Subnets"]
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "describe_subnets") from exc

    def find_availability_zones(self) -> list[str]:
        """The names of the availability zones that are currently available."""
        try:
            response = self._ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "describe_availability_zones") from exc
        return [
            zone["ZoneName"]
            for zone in response.get("AvailabilityZones", [])
            if zone.get("State") == "available"
        ]

    def create_vpc(self, cidr_block: str, tag_specifications: list[dict[str, Any]]) -> str:
        """Create a VPC and return its id."""
        try:
            response = self._ec2.create_vpc(CidrBlock=cidr_block, TagSpecifications=tag_specifications)
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "create_vpc") from exc
        return response["Vpc"]["VpcId"]

    def create_subnet(
        self,
        vpc_id: str,
        cidr_block: str,
        availability_zone: str,
        tag_specifications: list[dict[str, Any]],
    ) -> str:
        """Create a subnet in one availability zone and return its id."""
        try:
            response = self._ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=cidr_block,
                AvailabilityZone=availability_zone,
                TagSpecifications=tag_specifications,
            )
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "create_subnet") from exc
        return response["Subnet"]["SubnetId"]

    def enable_dns_support(self, vpc_id: str) -> None:
        """Turn on DNS resolution inside a VPC."""
        try:
            self._ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "modify_vpc_attribute") from exc

    def enable_dns_hostnames(self, vpc_id: str) -> None:
        """Turn on DNS hostnames inside a VPC."""
        try:
            self._ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "modify_vpc_attribute") from exc

    def delete_subnet(self, subnet_id: str) -> None:
        """Delete a subnet."""
        try:
            self._ec2.delete_subnet(SubnetId=subnet_id)
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "delete_subnet") from exc

    def delete_vpc(self, vpc_id: str) -> None:
        """Delete a VPC."""
        try:
            self._ec2.delete_vpc(VpcId=vpc_id)
        except (ParamValidationError, ClientError) as exc:
            raise as_provisioning_error(exc, "delete_vpc") from exc

    # ==================================================================
    # Provisioning.  Each step asks EC2 what already exists and creates
    # only what is missing, so running it twice is harmless.
    # ==================================================================

    def provision(self, record: RequestRecord) -> ResourceSet:
        """Build the network, reusing anything an earlier attempt already created."""
        request = record.request
        request_id = record.request_id

        vpc_id = self._ensure_vpc(request, request_id)
        self.enable_dns_support(vpc_id)
        self.enable_dns_hostnames(vpc_id)
        subnets = self._ensure_subnets(request, request_id, vpc_id)

        return ResourceSet(
            vpc_id=vpc_id,
            vpc_cidr_block=request.cidr_block,
            subnets=subnets,
        )

    def _ensure_vpc(self, request: VpcRequest, request_id: str) -> str:
        """The VPC for this request: reuse the tagged one, or create it."""
        existing = first_or_none(self.find_vpcs(self._ownership_filters(request_id)))
        if existing is not None:
            logger.info("reusing the existing vpc requestId=%s vpcId=%s", request_id, existing["VpcId"])
            return existing["VpcId"]

        vpc_id = self.create_vpc(
            request.cidr_block,
            self._tag_spec("vpc", request, request_id, "vpc", request.name),
        )
        logger.info("created vpc requestId=%s vpcId=%s", request_id, vpc_id)
        return vpc_id

    def _ensure_subnets(self, request: VpcRequest, request_id: str, vpc_id: str) -> list[SubnetResource]:
        """Every requested subnet: reuse the tagged ones, create the rest."""
        existing_by_name: dict[str, dict[str, Any]] = {}
        for item in self.find_subnets(self._ownership_filters(request_id)):
            existing_by_name[self._logical_id(item)] = item

        zones: list[str] = []
        subnets: list[SubnetResource] = []

        for index, spec in enumerate(request.subnets):
            existing = existing_by_name.get(spec.logical_id)
            if existing is not None:
                subnets.append(
                    SubnetResource(
                        logical_id=spec.logical_id,
                        name=spec.name,
                        subnet_id=existing["SubnetId"],
                        cidr_block=existing.get("CidrBlock") or spec.cidr_block,
                        availability_zone=existing.get("AvailabilityZone") or spec.availability_zone,
                    )
                )
                continue

            if not zones:
                zones = self._available_zones()
            availability_zone = spec.availability_zone or zones[index % len(zones)]

            subnet_id = self.create_subnet(
                vpc_id,
                spec.cidr_block,
                availability_zone,
                self._tag_spec("subnet", request, request_id, spec.logical_id, spec.name),
            )
            subnets.append(
                SubnetResource(
                    logical_id=spec.logical_id,
                    name=spec.name,
                    subnet_id=subnet_id,
                    cidr_block=spec.cidr_block,
                    availability_zone=availability_zone,
                )
            )

        logger.info("subnets ready requestId=%s count=%s", request_id, len(subnets))
        return subnets

    def _available_zones(self) -> list[str]:
        """The region's available zones, or a guess based on the region name."""
        try:
            zones = self.find_availability_zones()
            if zones:
                return sorted(zones)
        except ProvisioningError:
            logger.warning("could not list availability zones; falling back to region naming")
        return [f"{get_settings().region}{suffix}" for suffix in ("a", "b", "c")]

    # ==================================================================
    # Teardown
    # ==================================================================

    def rollback(self, request_id: str) -> list[str]:
        """Delete the request's resources innermost-first, and return the ids removed."""
        filters = self._ownership_filters(request_id)
        deleted: list[str] = []

        for subnet in self.find_subnets(filters):
            try:
                self.delete_subnet(subnet["SubnetId"])
                deleted.append(subnet["SubnetId"])
            except ProvisioningError as error:
                logger.warning("could not delete subnet id=%s detail=%s", subnet["SubnetId"], error.detail)

        for vpc in self.find_vpcs(filters):
            try:
                self.delete_vpc(vpc["VpcId"])
                deleted.append(vpc["VpcId"])
            except ProvisioningError as error:
                logger.warning("could not delete vpc id=%s detail=%s", vpc["VpcId"], error.detail)

        logger.info("rollback finished requestId=%s deletedCount=%s", request_id, len(deleted))
        return deleted

    # ==================================================================
    # Reporting
    # ==================================================================

    def list_owned_resources(self, request_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Every resource carrying our ManagedBy tag, grouped by kind."""
        filters = self._ownership_filters(request_id)
        return {
            "vpcs": self.find_vpcs(filters),
            "subnets": self.find_subnets(filters),
        }

    # ==================================================================
    # Tags and filters
    # ==================================================================

    def _tag_spec(self, resource_type: str, request: VpcRequest, request_id: str, logical_id: str, name: str) -> list[dict[str, Any]]:
        """The TagSpecifications entry that tags a resource when it is created."""
        tags = {
            **request.tags,
            "Name": name,
            TAG_MANAGED_BY: get_settings().managed_by_tag,
            TAG_REQUEST_ID: request_id,
            TAG_LOGICAL_ID: logical_id,
        }
        return [
            {
                "ResourceType": resource_type,
                "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
            }
        ]

    def _ownership_filters(self, request_id: str | None) -> list[dict[str, Any]]:
        """EC2 filters that select only the resources this service owns."""
        filters = [{"Name": f"tag:{TAG_MANAGED_BY}", "Values": [get_settings().managed_by_tag]}]
        if request_id:
            filters.append({"Name": f"tag:{TAG_REQUEST_ID}", "Values": [request_id]})
        return filters

    @staticmethod
    def _logical_id(item: dict[str, Any]) -> str:
        """Read the prov:logicalId tag off an EC2 resource."""
        return {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}.get(TAG_LOGICAL_ID, "")




