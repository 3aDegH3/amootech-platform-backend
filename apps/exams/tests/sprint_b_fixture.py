"""Small catalog assembled exclusively from captured read-only provider responses."""
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from apps.academics.local_catalog import sync_catalog


def recorded_catalog():
    folder = Path(__file__).parent / 'fixtures'
    automatic = json.loads((folder / 'automatic_catalog.json').read_text())
    reports = json.loads((folder / 'report_catalog.json').read_text())
    physics = next(row for row in automatic['courses']['result'] if row['courseId'] == 113 and row['gradeId'] == 11)
    return {'education_periods': {'status':100, 'result':[{'id':4, 'title':'دوره دوم متوسطه'}]},
            'grades': automatic['grades'], 'courses': {'status':100, 'result':[physics, *reports['courses']['result']]},
            'details': {'11:113':automatic['detail'], **{f'31:{key}':value for key,value in reports['details'].items()}},
            'inventory': automatic['provider_inventory']}


def publish_recorded_catalog():
    data = recorded_catalog()
    with patch('apps.academics.local_catalog.Quiz24Service') as service:
        for method in ('education_periods', 'grades', 'courses'):
            getattr(service.return_value, method).side_effect = lambda method=method: deepcopy(data[method])
        service.return_value.course_detail.side_effect = lambda **kwargs: deepcopy(data['details'][f"{kwargs['grade_id']}:{kwargs['course_id']}"])
        sync_catalog()
    return data
