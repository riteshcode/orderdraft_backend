"""
Saare destination types ek jagah. Naya type = file banao + yahan add karo.
"""
from app.modules.destinations import accounting_dest, custom_api, internal_invoice, webhook

DESTINATIONS = {
    accounting_dest.TYPE: accounting_dest,
    internal_invoice.TYPE: internal_invoice,
    custom_api.TYPE: custom_api,
    webhook.TYPE: webhook,
}


def get_module(dest_type: str):
    module = DESTINATIONS.get(dest_type)
    if not module:
        raise ValueError(f"Unknown destination type: {dest_type}")
    return module
