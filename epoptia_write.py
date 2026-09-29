"""Fail-closed admin-write foundation. No network, browser or credential loading.

A future reviewed transport must bind these relative routes to the authenticated
Epoptia origin, reject redirects, require a session and CSRF protection, and read
the current field immediately before writing. No write transport exists here.
"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Contract:
    id_field: str
    expected_field: str
    new_field: str
    route: str
    form_field: str
    limit: int


CONTRACTS = MappingProxyType({
    "update_product_name": Contract("product_id", "expected_current_name", "new_name",
                                    "/products/update/{id}", "name", 255),
    "update_wol_description": Contract("wol_id", "expected_current_description", "new_description",
                                       "/workorderlines/update/{id}", "description", 2000),
})


def validate_write(name, arguments):
    contract = CONTRACTS[name]
    if type(arguments) is not dict or set(arguments) != {
            contract.id_field, contract.expected_field, contract.new_field}:
        raise ValueError("Invalid write arguments")
    identifier = arguments[contract.id_field]
    if type(identifier) is not int or not 1 <= identifier <= 999999999999999:
        raise ValueError("Invalid target")
    for field in (contract.expected_field, contract.new_field):
        value = arguments[field]
        if (type(value) is not str or not value.strip() or len(value) > contract.limit
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("Invalid field value")
    return contract


async def execute_write(name, arguments, *, read_transport=None):
    """Return only audit-safe labels. The optional transport is read-only.

    read_transport is an internal future integration seam, never caller input:
    authenticated and csrf_ready are booleans; read_current(action, id) returns
    the exact current string. Even a valid session and matching read cannot
    enable writes: this module has no send/POST method or success path.
    """
    contract = validate_write(name, arguments)
    status = "auth_required"
    try:
        if (read_transport is not None and read_transport.authenticated is True
                and read_transport.csrf_ready is True):
            current = await read_transport.read_current(name, arguments[contract.id_field])
            if type(current) is not str:
                status = "read_unverified"
            elif current != arguments[contract.expected_field]:
                status = "conflict"
            else:
                status = "write_transport_unverified"
    except Exception:
        # Never echo transport exceptions, raw records, headers or session data.
        status = "read_unverified"
    return {"ok": False, "status": status, "action": name,
            "target_id": arguments[contract.id_field], "write_performed": False}
