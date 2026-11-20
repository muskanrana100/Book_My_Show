from django import forms
from django.core.exceptions import ValidationError

from .models import Movie, Review, ReviewReport
from .validators import parse_youtube_id

REVIEW_BODY_MIN = 5
REVIEW_BODY_MAX = 2000
REVIEW_TITLE_MAX = 150
REPORT_NOTE_MAX = 500


class ReviewForm(forms.ModelForm):
    body = forms.CharField(
        min_length=REVIEW_BODY_MIN,
        max_length=REVIEW_BODY_MAX,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 4,
            'placeholder': f'What did you think of the movie? (at least {REVIEW_BODY_MIN} characters)',
            'maxlength': REVIEW_BODY_MAX,
        }),
        error_messages={
            'min_length': f'Please write at least {REVIEW_BODY_MIN} characters.',
            'max_length': f'Reviews can be at most {REVIEW_BODY_MAX} characters.',
            'required': 'Please write your review.',
        },
    )
    title = forms.CharField(
        max_length=REVIEW_TITLE_MAX, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Sum up your review in a few words',
            'maxlength': REVIEW_TITLE_MAX,
        }),
    )

    class Meta:
        model = Review
        fields = ['rating', 'title', 'body']
        widgets = {
            'rating': forms.RadioSelect(),
        }
        error_messages = {
            'rating': {'required': 'Please choose a rating from 1 to 5 stars.',
                       'invalid_choice': 'Please choose a rating from 1 to 5 stars.'},
        }

    def clean_body(self):
        body = (self.cleaned_data.get('body') or '').strip()
        if len(body) < REVIEW_BODY_MIN:
            raise ValidationError(f'Please write at least {REVIEW_BODY_MIN} characters.')
        return body


class ReviewReportForm(forms.ModelForm):
    note = forms.CharField(
        required=False, max_length=REPORT_NOTE_MAX,
        widget=forms.Textarea(attrs={
            'class': 'form-control', 'rows': 2, 'placeholder': 'Optional details',
            'maxlength': REPORT_NOTE_MAX,
        }),
    )

    class Meta:
        model = ReviewReport
        fields = ['reason', 'note']
        widgets = {
            'reason': forms.Select(attrs={'class': 'form-control'}),
        }


class MovieAdminForm(forms.ModelForm):
    """
    Lets an administrator paste a YouTube link or a bare video ID. The value is parsed on the
    server; anything that is not a supported YouTube reference is rejected before it is stored.
    """
    trailer_youtube_id = forms.CharField(
        required=False,
        max_length=300,
        label='Trailer (YouTube link or video ID)',
        help_text='Paste a YouTube link (youtube.com/watch?v=..., youtu.be/...) or the 11-character video ID.',
    )

    class Meta:
        model = Movie
        fields = '__all__'

    def clean_trailer_youtube_id(self):
        raw = self.cleaned_data.get('trailer_youtube_id', '')
        video_id = parse_youtube_id(raw)
        if video_id is None:
            raise ValidationError(
                'Enter a YouTube link (youtube.com/watch?v=..., youtu.be/..., youtube.com/embed/...) '
                'or the 11-character video ID. Other links are not accepted.'
            )
        return video_id
