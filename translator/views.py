import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from .services import LANG_BY_CODE, LANGUAGES, MAX_CHARS, TranslationError, translate_text

DEFAULT_SOURCE = "auto"
DEFAULT_TARGET = "hi"


def home(request):
    """Main page. Also handles a normal form POST if JavaScript is unavailable."""
    context = {
        "languages": LANGUAGES,
        "max_chars": MAX_CHARS,
        "text": "",
        "selected_source": DEFAULT_SOURCE,
        "selected_target": DEFAULT_TARGET,
    }

    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        source = request.POST.get("source_language", DEFAULT_SOURCE)
        target = request.POST.get("target_language", DEFAULT_TARGET)
        context.update(text=text, selected_source=source, selected_target=target)

        try:
            result = translate_text(text, source, target)
            context.update(
                translated_text=result["translated"],
                detected_language=" + ".join(result["detected"]),
                target_name=LANG_BY_CODE[target]["name"],
            )
        except TranslationError as exc:
            context["error"] = str(exc)

    return render(request, "translator/home.html", context)


@require_POST
def translate_api(request):
    """JSON endpoint used by the page so translating doesn't reload it."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request."}, status=400)

    text = str(payload.get("text", ""))
    source = str(payload.get("source", DEFAULT_SOURCE))
    target = str(payload.get("target", DEFAULT_TARGET))

    try:
        result = translate_text(text, source, target)
    except TranslationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({
        "translated": result["translated"],
        "detected": result["detected"],
        "target_name": LANG_BY_CODE[target]["name"],
    })
