def extract_tokens_robust(llm_resp) -> tuple[int, int, int]:
    """
    Robustly extract (prompt_tokens, completion_tokens, total_tokens) from LLMResponse,
    supporting standard properties, nested dict/objects inside usage, and raw completions.
    """
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    if not llm_resp:
        return 0, 0, 0

    # 1. Try to read from standard llm_resp.usage
    usage = getattr(llm_resp, "usage", None)
    if usage:
        # Standard TokenUsage object has input, output, total properties
        if hasattr(usage, "input"):
            prompt_tokens = getattr(usage, "input") or 0
        elif hasattr(usage, "input_other"):
            # Direct attributes in case standard properties aren't accessed
            input_other = getattr(usage, "input_other", 0) or 0
            input_cached = getattr(usage, "input_cached", 0) or 0
            prompt_tokens = input_other + input_cached

        if hasattr(usage, "output"):
            completion_tokens = getattr(usage, "output") or 0
        if hasattr(usage, "total"):
            total_tokens = getattr(usage, "total") or 0

        # If the values are still 0, maybe usage was a dictionary or some other object
        if prompt_tokens == 0 and completion_tokens == 0:
            if isinstance(usage, dict):
                prompt_tokens = (
                    usage.get("prompt_tokens")
                    or usage.get("input_tokens")
                    or usage.get("input")
                    or 0
                )
                completion_tokens = (
                    usage.get("completion_tokens")
                    or usage.get("output_tokens")
                    or usage.get("output")
                    or 0
                )
                total_tokens = (
                    usage.get("total_tokens")
                    or usage.get("total")
                    or (prompt_tokens + completion_tokens)
                )

    # 2. Secondary fallback: check raw_completion
    if prompt_tokens == 0 and completion_tokens == 0:
        raw = getattr(llm_resp, "raw_completion", None)
        if raw:
            raw_usage = None
            if isinstance(raw, dict):
                raw_usage = raw.get("usage")
            elif hasattr(raw, "usage"):
                raw_usage = getattr(raw, "usage")

            if raw_usage:
                if isinstance(raw_usage, dict):
                    prompt_tokens = (
                        raw_usage.get("prompt_tokens")
                        or raw_usage.get("input_tokens")
                        or raw_usage.get("prompt_token_count")
                        or raw_usage.get("input")
                        or 0
                    )
                    completion_tokens = (
                        raw_usage.get("completion_tokens")
                        or raw_usage.get("output_tokens")
                        or raw_usage.get("candidates_token_count")
                        or raw_usage.get("output")
                        or 0
                    )
                    total_tokens = (
                        raw_usage.get("total_tokens")
                        or raw_usage.get("total")
                        or (prompt_tokens + completion_tokens)
                    )
                else:
                    prompt_tokens = (
                        getattr(raw_usage, "prompt_tokens", 0)
                        or getattr(raw_usage, "input_tokens", 0)
                        or getattr(raw_usage, "prompt_token_count", 0)
                        or getattr(raw_usage, "input", 0)
                        or 0
                    )
                    completion_tokens = (
                        getattr(raw_usage, "completion_tokens", 0)
                        or getattr(raw_usage, "output_tokens", 0)
                        or getattr(raw_usage, "candidates_token_count", 0)
                        or getattr(raw_usage, "output", 0)
                        or 0
                    )
                    total_tokens = (
                        getattr(raw_usage, "total_tokens", 0)
                        or getattr(raw_usage, "total", 0)
                        or (prompt_tokens + completion_tokens)
                    )

    return prompt_tokens, completion_tokens, total_tokens
