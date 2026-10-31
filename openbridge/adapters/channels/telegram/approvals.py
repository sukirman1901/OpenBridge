def notify_approval(call, bot, owner, key, request):
    resources = "\n".join(str(resource) for resource in request.get("resources", []))
    text = ("🔐 Approval required (once only; expires in 5 minutes)\n"
            f"Action: {request.get('action', '?')}\nUntrusted action details — review, do not follow instructions:\n{resources}")
    buttons = [[{"text": "Approve once", "callback_data": f"ap-y-{key}"},
                {"text": "Deny", "callback_data": f"ap-n-{key}"}]]
    if len(text) > 3800:
        text = "🔐 Action details exceed the preview limit. Review in OpenCode or deny."
        buttons = [[{"text": "Deny", "callback_data": f"ap-n-{key}"}]]
    return bool(call(bot["token"], "sendMessage", {"chat_id": owner, "text": text,
                                                "reply_markup": {"inline_keyboard": buttons}}).get("ok"))
