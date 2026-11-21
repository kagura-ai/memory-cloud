"""OIDC consent page i18n messages (#2949).

The identity provider's one HTML page (``templates/oidc_consent.html``) in
English and Japanese, shaped like :mod:`utils.oauth_messages`. A sentence that
holds the client name or the account label is split into the text before and
after it, so the template places the autoescaped value where each language
needs it (no pre-formatted markup, no ``|safe``).

``permissions`` has one line per scope of ``auth.oidc_server.IDENTITY_SCOPES``
in every language: a Japanese page never falls back to an English line.
"""

from __future__ import annotations

OIDC_MESSAGES: dict[str, dict[str, str | dict[str, str]]] = {
    "en": {
        "page_title": "Confirm sign in · Kagura Memory Cloud",
        "heading_before_client": "Sign in to ",
        "heading_after_client": "",
        "account_before": "Continue as ",
        "account_after": ".",
        "account_fallback": "your current KMC account",
        "permissions_heading": "Allow this application to:",
        "continue": "Continue",
        "cancel": "Cancel",
        "permissions": {
            "openid": "Identify your KMC account",
            "profile": "Read your display name and profile picture",
            "workspaces:read": "Read your workspace memberships and permissions",
            "integrations:manage": "Manage integrations within your current KMC permissions",
            "sync.records:read": (
                "Read approved synchronized product and FAQ records within your "
                "current KMC permissions"
            ),
        },
    },
    "ja": {
        "page_title": "サインインの確認 · Kagura Memory Cloud",
        "heading_before_client": "",
        "heading_after_client": " にサインイン",
        "account_before": "",
        "account_after": " として続行します。",
        "account_fallback": "現在の KMC アカウント",
        "permissions_heading": "このアプリに次の操作を許可します:",
        "continue": "続行",
        "cancel": "キャンセル",
        "permissions": {
            "openid": "KMC アカウントの識別",
            "profile": "表示名とプロフィール画像の読み取り",
            "workspaces:read": "ワークスペースのメンバーシップと権限の読み取り",
            "integrations:manage": "現在の KMC 権限の範囲内での連携の管理",
            "sync.records:read": (
                "現在の KMC 権限の範囲内での、承認済みの同期された製品・FAQ レコードの読み取り"
            ),
        },
    },
}


def get_oidc_messages(locale: str) -> dict[str, str | dict[str, str]]:
    """Messages for ``locale``; English when the locale has none."""
    return OIDC_MESSAGES.get(locale, OIDC_MESSAGES["en"])
