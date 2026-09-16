
from django import forms
from .models import VehicleDocument, InsuranceDocument

class VehicleDocumentForm(forms.ModelForm):
    class Meta:
        model = VehicleDocument
        fields = '__all__'

class InsuranceDocumentForm(forms.ModelForm):
    class Meta:
        model = InsuranceDocument
        fields = '__all__'

