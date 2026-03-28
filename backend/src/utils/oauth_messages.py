"""OAuth2 Authorization Page i18n Messages.

Issue #221: Internationalization support for OAuth authorization page.
Supports English and Japanese.
"""

OAUTH_MESSAGES: dict[str, dict[str, str | dict[str, str]]] = {
    "en": {
        "title": "Authorization Request",
        "subtitle": "Allow this app to access your Kagura Memory Cloud",
        "authorize": "Authorize",
        "cancel": "Cancel",
        "signing_in_as": "Signing in as",
        "badge_text": "Secure Authorization",
        "permissions": {
            "read": "Read your memories",
            "write": "Write new memories",
            "manage": "Manage your memory cloud",
        },
    },
    "ja": {
        "title": "認可リクエスト",
        "subtitle": "このアプリがKagura Memory Cloudにアクセスすることを許可",
        "authorize": "認可",
        "cancel": "キャンセル",
        "signing_in_as": "ログイン中",
        "badge_text": "セキュア認可",
        "permissions": {
            "read": "メモリーの読み取り",
            "write": "新しいメモリーの作成",
            "manage": "メモリークラウドの管理",
        },
    },
}


def get_oauth_messages(locale: str) -> dict[str, str | dict[str, str]]:
    """Get OAuth messages for the specified locale.

    Args:
        locale: Language code (e.g., "en", "ja")

    Returns:
        Messages dictionary for the locale, falls back to English if not found
    """
    return OAUTH_MESSAGES.get(locale, OAUTH_MESSAGES["en"])
