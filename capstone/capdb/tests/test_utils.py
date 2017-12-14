import pytest
from django.utils.text import slugify


@pytest.mark.django_db
def test_generate_unique_slug_manually(case):
    old_slug = case.slug
    case.pk = None
    case.slug = None
    case.save(slug_base=old_slug)
    assert case.slug == old_slug + '-1'

@pytest.mark.django_db
def test_generate_unique_slug_automatically(court):
    old_slug = court.slug
    assert old_slug == slugify(court.name_abbreviation)
    court.pk = None
    court.slug = None
    court.save()
    assert court.slug == old_slug + '-1'
