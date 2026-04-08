"""OAuth2 Authorization Page i18n Messages.

Issue #221: Internationalization support for OAuth authorization page.
Issue #218: Error page messages for invalid redirect_uri pre-check.
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
        "error_title": "Authorization Error",
        "error_badge": "Request Blocked",
        "error_invalid_redirect_uri_heading": "This authorization request was blocked",
        "error_invalid_redirect_uri_body": (
            "The redirect_uri in this request is not registered with the OAuth "
            "client. To protect you from phishing, the consent screen was not "
            "shown."
        ),
        "error_what_to_do": (
            "Ask the OAuth client administrator to register this redirect_uri in "
            "the Kagura Memory Cloud admin panel, then try again."
        ),
        "error_redirect_uri_label": "Attempted redirect_uri",
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
        "error_title": "認可エラー",
        "error_badge": "リクエストをブロックしました",
        "error_invalid_redirect_uri_heading": "この認可リクエストはブロックされました",
        "error_invalid_redirect_uri_body": (
            "リクエストの redirect_uri が OAuth クライアントに登録されていません。"
            "フィッシングからユーザーを保護するため、認可画面は表示されませんでした。"
        ),
        "error_what_to_do": (
            "OAuth クライアント管理者に依頼して、Kagura Memory Cloud の管理画面で"
            "この redirect_uri を登録してから、再度お試しください。"
        ),
        "error_redirect_uri_label": "リクエストされた redirect_uri",
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
