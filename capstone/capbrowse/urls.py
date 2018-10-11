from . import views
from django.urls import path

urlpatterns = [
    ### pages ###
    path('browse/cases/<int:case_id>/', views.browse_case, name='browse_case'),
    path('browse/cases/', views.browse_cases, name='browse_cases'),
]

