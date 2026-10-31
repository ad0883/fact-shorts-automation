"""Read-only Meta credential diagnostics; never prints access tokens."""
import os
from pipeline import publish_instagram as ig, publish_facebook as fb


def check():
    ok = True
    for name, module in [('Instagram', ig), ('Facebook', fb)]:
        try:
            account, token, root = module.config()
            if name == 'Instagram':
                result = ig.api('GET', root, account, token, params={'fields': 'id,username'})
                print(f"Instagram account verified: {result.get('username', result.get('id'))}")

            else:
                result = fb.request('GET', root+'/me', token, params={'fields': 'id,name'})
                if str(result.get('id')) != account:
                    raise ValueError('Page token belongs to another Page; fix FACEBOOK_PAGE_ID/token.')
                print(f"Facebook Page verified: {result.get('name', account)}")
        except Exception as exc:
            ok = False
            print(f'{name}: {exc}')
    if not os.environ.get('INSTAGRAM_MEDIA_BASE_URL', '').strip():
        ok = False
        print('Instagram media hosting: set INSTAGRAM_MEDIA_BASE_URL and mirror exports there, or provide per-run instagram_media_url.json files.')
    print('Read-only checks do not prove publishing permissions; those are checked by Meta on upload.')
    return 0 if ok else 1
