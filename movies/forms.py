from django import forms
from .models import Review, ReviewReport

class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['rating', 'title','body']
        widgets = {
            'rating': forms.RadioSelect(),
            'title': forms.TextInput(attrs ={
                'class': "form-control",
                'placeholder':'Sum up your review in a few words'}),
            'body': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'What did you think of the movie?',
            }),
       }
       
class ReviewReportForm(forms.ModelForm):
    class Meta:
        model = ReviewReport
        fields = ['reason', 'note']
        widgets = {
            'reason': forms.Select(attrs={'class': 'form-control'}),
            'note': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2, 'placeholder': 'Optional details',
            }),
        }