#!/usr/bin/env python3
"""Push branch and create PR using GitHub App installation token."""
import jwt, time, requests, subprocess, os, sys

PEM_FILE = '/root/.hermes/github-app.pem'
APP_ID = '3910462'
REPO = 'Axighi/atlaz'
BRANCH = 'fix/docs-rebrand-issue11'

with open(PEM_FILE) as f: key = f.read()
now = int(time.time())
app_token = jwt.encode({'iat': now - 60, 'exp': now + 600, 'iss': APP_ID}, key, algorithm='RS256')
headers = {'Authorization': f'Bearer {app_token}', 'Accept': 'application/vnd.github+json'}
inst_id = requests.get('https://api.github.com/app/installations', headers=headers).json()[0]['id']
resp = requests.post(f'https://api.github.com/app/installations/{inst_id}/access_tokens', headers=headers).json()
token = resp['token']

env = os.environ.copy()
env['GITHUB_TOKEN'] = token

# Update remote with actual token
subprocess.run(['git', 'remote', 'set-url', 'origin',
    f'https://x-access-token:***@github.com/{REPO}.git'],
    cwd='/root/projects/atlaz')

push = subprocess.run(['git', 'push', 'origin', BRANCH, '--force'],
    capture_output=True, text=True, cwd='/root/projects/atlaz', env=env)
print("PUSH:", (push.stdout or '')[-300:])
print("PUSH_ERR:", (push.stderr or '')[-300:])
if push.returncode != 0:
    print("FAILED - trying with gh auth...")
    pr_body = 'Closes #11\n\nUpdates all user-facing documentation:\n- README.md, README.zh-CN.md, CONTRIBUTING.md, SECURITY.md, AGENTS.md\n- website/docs/, website/src/pages/\n- Website config files'
    pr = subprocess.run(['gh', 'pr', 'create', '--repo', REPO,
        '--title', 'rebrand: update documentation files (#11)',
        '--body', pr_body, '--base', 'main', '--head', BRANCH],
        capture_output=True, text=True, cwd='/root/projects/atlaz', env=env)
    print("GH_PR:", pr.stdout)
    print("GH_PR_ERR:", pr.stderr)
    sys.exit(1)

# Restore original remote URL (with masked token)
subprocess.run(['git', 'remote', 'set-url', 'origin',
    'https://x-access-token:***@github.com/Axighi/atlaz.git'],
    cwd='/root/projects/atlaz')

pr_body = 'Closes #11\n\nUpdates all user-facing documentation:\n- README.md, README.zh-CN.md, CONTRIBUTING.md, SECURITY.md, AGENTS.md\n- website/docs/, website/src/pages/\n- Website config files'
pr = subprocess.run(['gh', 'pr', 'create', '--repo', REPO,
    '--title', 'rebrand: update documentation files (#11)',
    '--body', pr_body, '--base', 'main', '--head', BRANCH],
    capture_output=True, text=True, cwd='/root/projects/atlaz', env=env)
print("PR:", pr.stdout)
print("PR_ERR:", pr.stderr)
if pr.returncode != 0:
    print(f"PR creation failed: {pr.returncode}")
    sys.exit(1)

label = subprocess.run(['gh', 'pr', 'edit', '--repo', REPO, '--add-label', 'rebrand,docs'],
    capture_output=True, text=True, cwd='/root/projects/atlaz', env=env)
print("LABEL:", label.stdout)
if label.stderr: print("LABEL_ERR:", label.stderr)
print(f"\nSuccess! PR created at: {pr.stdout.strip()}")
