"""Validate final answers without grading hidden reasoning or dropping questions."""
from copy import deepcopy


def scoring_response(response):
    """Return a scoring copy and whether reasoning exhausted the answer budget."""
    if not isinstance(response, dict) or response.get('error'):
        raise ValueError('invalid API response')
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError('expected exactly one API choice')
    choice = choices[0]
    if not isinstance(choice, dict) or type(choice.get('index')) is not int or choice['index'] != 0:
        raise ValueError('invalid API choice index')
    message = choice.get('message')
    if not isinstance(message, dict) or 'content' not in message:
        raise ValueError('missing API message content')
    content = message['content']
    if content is not None and not isinstance(content, str):
        raise ValueError('invalid API message content')
    if choice.get('finish_reason') not in ('stop', 'length'):
        raise ValueError('invalid API finish reason')
    exhausted = not (content or '').strip()
    if exhausted:
        reasoning = any(isinstance(message.get(key), str) and message[key].strip()
                        for key in ('reasoning', 'reasoning_content'))
        usage = response.get('usage')
        tokens = usage.get('completion_tokens') if isinstance(usage, dict) else None
        if choice['finish_reason'] != 'length' or not reasoning or type(tokens) is not int or tokens <= 0:
            raise ValueError('missing final answer in API response; inspect reasoning/parser configuration')
    normalized = deepcopy(response)
    if exhausted:
        # A literal empty answer scores upstream as unanswered, even when the
        # harness environment supplies a placeholder for Python None responses.
        normalized['choices'][0]['message']['content'] = ''
    return normalized, exhausted
