"""AI-powered accounts-payable invoice processor.

Pipeline: ingest -> mask PII -> extract (LLM) -> validate -> agent decision -> store + audit.
"""

__version__ = "0.1.0"
