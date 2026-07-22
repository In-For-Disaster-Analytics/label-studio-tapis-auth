FROM heartexlabs/label-studio:latest

RUN pip install --no-cache-dir pyjwt

COPY tapis_auth /label-studio/label_studio/tapis_auth
COPY settings_additions.py /tmp/settings_additions.py
COPY urls_additions.py /tmp/urls_additions.py

# Append to the same settings/urls modules Label Studio already uses, rather
# than introducing a separate DJANGO_SETTINGS_MODULE/ROOT_URLCONF — Label
# Studio's own core/settings/label_studio.py reads back from django.conf.settings
# mid-import (sentry.init_sentry() -> settings.SENTRY_DSN), which only resolves
# correctly if the settings module Django loads is the exact same file whose
# partial execution already defined that name — true for a same-file append,
# not true if a wrapper module imports it via `from ... import *`.
RUN cat /tmp/settings_additions.py >> /label-studio/label_studio/core/settings/label_studio.py \
    && cat /tmp/urls_additions.py >> /label-studio/label_studio/core/urls.py
