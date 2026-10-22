"""One title rule for both independent taxonomy namespaces."""
def build_title(selection, free_title=''):
    labels = selection.get('labels', {})
    course = labels.get('course', '')
    deepest = next((labels[k] for k in ('object', 'subject', 'topic', 'chapter') if labels.get(k)), '')
    if course and deepest and course != deepest:
        return f'{course} — {deepest}'
    return course or deepest or labels.get('grade', '') or free_title
