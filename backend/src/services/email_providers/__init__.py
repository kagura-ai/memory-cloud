"""Concrete EmailService backends (Issue #478).

The default ``LoggingEmailService`` lives in ``services/email_service.py``
because it is the closed-beta default and ships in every deployment. Real
provider integrations (currently only Resend) live in this subpackage so
they can be lazy-imported by ``get_email_service()``, which means their
SDK is not imported or initialized unless that provider is selected. The
SDK package itself is still installed as a regular dependency — moving it
to an optional extra is a follow-up if the cost of carrying it becomes
material.
"""
