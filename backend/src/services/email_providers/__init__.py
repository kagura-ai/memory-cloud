"""Concrete EmailService backends (Issue #478).

The default ``LoggingEmailService`` lives in ``services/email_service.py``
because it is the closed-beta default and ships in every deployment. Real
provider integrations (currently only Resend) live in this subpackage so
they can be lazy-imported by ``get_email_service()`` and never pull their
SDK into deployments that stay on the logging stub.
"""
