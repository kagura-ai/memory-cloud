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
            "Kagura Memory Cloud could not authorize this request. The "
            "redirect_uri is missing, malformed, or not registered with the "
            "OAuth client. To protect you from phishing, the consent screen "
            "was not shown."
        ),
        "error_what_to_do": (
            "If you are the OAuth client administrator, register this "
            "redirect_uri in the Kagura Memory Cloud admin panel and try "
            "again. If you arrived here from another app, ask its developer "
            "to update the redirect_uri it sends to Kagura."
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
            "Kagura Memory Cloud はこのリクエストを認可できませんでした。"
            "redirect_uri が指定されていない、形式が不正、または OAuth クライアントに"
            "登録されていません。フィッシングからユーザーを保護するため、認可画面は"
            "表示されませんでした。"
        ),
        "error_what_to_do": (
            "あなたが OAuth クライアントの管理者であれば、Kagura Memory Cloud の"
            "管理画面でこの redirect_uri を登録してから再度お試しください。"
            "別のアプリから来た場合は、そのアプリの開発者に連絡して、Kagura に送る"
            "redirect_uri を修正してもらってください。"
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
