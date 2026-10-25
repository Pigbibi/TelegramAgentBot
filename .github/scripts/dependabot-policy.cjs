const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');

const sensitive = /(?:^|[/@.:-])(?:spring\w*|security|oauth\w*|auth\w*|(?:py)?jwt\w*|jsonwebtoken|passport|crypto\w*|gradle|kotlin|vite|webpack|playwright|puppeteer|electron|wrangler|esbuild|ruff|pip|setuptools|uvicorn|fastapi|django|flask|react(?:-dom)?|next|nuxt|vue|svelte|hatchling|pyright|pytest(?:-\w+)?|miniflare|workers(?:-\w+)*|itsdangerous)(?:$|[/@.:-])/i;
const paused = new Set(['Pigbibi/SmsPressureCluster', 'Pigbibi/FranchiseLeadWorkflows']);
function inScope(config, repo) {
  return config.repo === repo && /^Pigbibi\/[^/]+$/.test(repo) && !paused.has(repo) && config.branch === 'main';
}
function dependencyFilesAllowed(config, repo, files) {
  return inScope(config, repo) && files.length > 0 && files.every(f =>
    ['modified'].includes(f.status) && config.files.includes(f.filename) && !f.previous_filename);
}
function trustedPR(config, repo, pr, head, commits) {
  return inScope(config, repo) && /^[a-f0-9]{40}$/.test(head || '') && pr.user?.login === 'dependabot[bot]' &&
    pr.state === 'open' && !pr.draft && pr.base?.ref === config.branch &&
    pr.base?.repo?.full_name === repo && pr.head?.repo?.full_name === repo && pr.head.sha === head &&
    commits.length > 0 && commits.length === pr.commits && commits.at(-1).sha === head &&
    commits.every(c => c.author?.login === 'dependabot[bot]' && c.commit?.verification?.verified === true);
}
function eligible({config, repo, pr, eventHead, dependencies, files, commits}) {
  if (!trustedPR(config, repo, pr, eventHead, commits) || files.length !== pr.changed_files || !dependencyFilesAllowed(config, repo, files)) return false;
  if (!Array.isArray(dependencies) || dependencies.length === 0) return false;
  return dependencies.every(d => {
    if (!['pip', 'npm_and_yarn', 'npm', 'gradle'].includes(d.packageEcosystem) || !config.directories.includes(d.directory)) return false;
    if (d.targetBranch !== config.branch || d.updateType !== 'version-update:semver-patch' || d.maintainerChanges !== false) return false;
    if (!d.dependencyName || sensitive.test(d.dependencyName)) return false;
    const previous = /^v?([1-9]\d*)\.(\d+)\.(\d+)$/.exec(d.prevVersion || '');
    const next = /^v?([1-9]\d*)\.(\d+)\.(\d+)$/.exec(d.newVersion || '');
    return Boolean(previous && next && previous[1] === next[1] && previous[2] === next[2] && BigInt(next[3]) > BigInt(previous[3]));
  });
}
function checksReady(checks, names, sha) {
  if (!names.includes('Repository hygiene') || !names.includes('Safe dependency update')) return false;
  if (checks.some(c => c.status !== 'completed' || !['success', 'skipped', 'neutral'].includes(c.conclusion))) return false;
  return names.every(name => {
    const matching = checks.filter(c => c.name === name && c.app?.id === 15368 && c.head_sha === sha);
    // A commit may have both push and PR runs. Every current matching record must pass.
    return matching.length > 0 && matching.every(c => c.status === 'completed' && c.conclusion === 'success');
  });
}
function mergeArguments(repo, number, sha) {
  return ['pr', 'merge', String(number), '--repo', repo, '--merge', '--delete-branch', '--match-head-commit', sha];
}
function gh(args) {return execFileSync('gh', args, {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}).trim();}
function api(route) {return JSON.parse(gh(['api', route]));}
function pages(route) {return JSON.parse(gh(['api', '--paginate', '--slurp', route])).flat();}
function main(mode) {
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, 'dependabot-policy.json'), 'utf8'));
  const event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
  const repo = process.env.GITHUB_REPOSITORY;
  if (!inScope(config, repo)) throw new Error('Repository outside approved scope');
  if (mode === 'metadata') {
    const number = event.pull_request?.number;
    if (!Number.isSafeInteger(number)) throw new Error('Missing PR number');
    const pr = api(`repos/${repo}/pulls/${number}`);
    const files = pages(`repos/${repo}/pulls/${number}/files?per_page=100`);
    const commits = pages(`repos/${repo}/pulls/${number}/commits?per_page=100`);
    let dependencies;
    try {dependencies = JSON.parse(process.env.DEPENDABOT_DEPENDENCIES || 'null');} catch {dependencies = null;}
    const safe = eligible({config, repo, pr, files, commits, dependencies, eventHead: event.pull_request.head.sha});
    fs.appendFileSync(process.env.GITHUB_OUTPUT, `eligible=${safe}\n`);
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, safe ? 'Verified patch; current CI must pass before merge.\n' : 'Review required: unknown, sensitive, tooling or non-patch update.\n');
    return;
  }
  if (mode !== 'merge') throw new Error('Expected metadata or merge command');
  const run = event.workflow_run;
  if (run?.event !== 'pull_request' || run.conclusion !== 'success' || run.repository?.full_name !== repo) return;
  let numbers = (run.pull_requests || []).map(p => p.number);
  if (!numbers.length) numbers = pages(`repos/${repo}/commits/${run.head_sha}/pulls?per_page=100`).map(p => p.number);
  numbers = [...new Set(numbers)];
  if (numbers.length !== 1 || !Number.isSafeInteger(numbers[0])) return;
  const number = numbers[0];
  const pr = api(`repos/${repo}/pulls/${number}`);
  const commits = pages(`repos/${repo}/pulls/${number}/commits?per_page=100`);
  if (!trustedPR(config, repo, pr, run.head_sha, commits) || pr.mergeable !== true || pr.mergeable_state !== 'clean') return;
  const files = pages(`repos/${repo}/pulls/${number}/files?per_page=100`);
  if (files.length !== pr.changed_files || !dependencyFilesAllowed(config, repo, files)) return;
  const branch = () => api(`repos/${repo}/branches/${config.branch}`).commit.sha;
  const base = branch();
  // Private repositories may lack server-side strict protection; require a current base explicitly.
  if (pr.base.sha !== base || api(`repos/${repo}/compare/${base}...${pr.head.sha}`).status !== 'ahead') return;
  const checkPages = JSON.parse(gh(['api', '--paginate', '--slurp', `repos/${repo}/commits/${pr.head.sha}/check-runs?filter=latest&per_page=100`]));
  if (!checksReady(checkPages.flatMap(p => p.check_runs || []), config.required_checks, pr.head.sha)) return;
  const status = api(`repos/${repo}/commits/${pr.head.sha}/status`);
  // Legacy status contexts are never silently ignored, including pending/error results.
  if (!Array.isArray(status.statuses) || status.statuses.length && status.state !== 'success') return;
  if (branch() !== base) return;
  gh(mergeArguments(repo, number, pr.head.sha));
  console.log(`Merged verified Dependabot PR #${number} at ${pr.head.sha}.`);
}
module.exports = {eligible, trustedPR, inScope, dependencyFilesAllowed, checksReady, mergeArguments};
if (require.main === module) {
  try {main(process.argv[2]);} catch {console.error('Dependency policy failed or outcome is unknown; inspect the PR before retrying.'); process.exitCode = 1;}
}
