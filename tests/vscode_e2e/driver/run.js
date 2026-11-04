'use strict';
// Runs each planned task in this window and answers its quick-picks the way a person
// would: accept the first row (ticking it first where the pick is a checklist), or press
// Escape. What VS Code then resolved is written by the task itself -- every task's
// command was replaced by `receipt.py` when the workspace was rendered -- so this file
// only drives and times; `tests/test_vscode_tasks_e2e.py` judges.
//
// Blind on purpose: the extension API cannot see a quick-pick another extension opened,
// so every step sends its keys whether one is showing or not. Accepting or escaping with
// nothing open is a no-op, and both input extensions draw their pick only once its rows
// exist, so a key cannot land on a list still loading.

const fs = require('fs');
const vscode = require('vscode');

const PLAN = JSON.parse(fs.readFileSync(process.env.DEVKIT_VSCODE_E2E_PLAN, 'utf8'));

// A checklist opens with focus in its filter box and no row active, so a bare accept
// takes nothing -- and `rioj7.command-variable` reads nothing as a cancel. So every step
// activates the first row, ticks it (a no-op on a single pick), and accepts. A row a
// remembered pick had already ticked is unticked by this, the accept is then refused for
// picking none, and the next step ticks it again: the loop heals itself.
const FIRST = 'quickInput.first';
const TOGGLE = 'quickInput.toggleCheckbox';
const ACCEPT = 'quickInput.accept';
const ESCAPE = 'workbench.action.closeQuickOpen';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// What each key's command refused, by command: a key sent with nothing to act on is the
// point of driving blind, so a refusal is counted into the report, not raised -- and a
// count that grows with every step is what a renamed command looks like.
const refused = {};

function press(command) {
  return vscode.commands.executeCommand(command).then(
    () => true,
    (err) => {
      const said = String((err && err.message) || err);
      refused[command] = refused[command] || { count: 0, last: '' };
      refused[command].count += 1;
      refused[command].last = said;
      return false;
    },
  );
}

async function drive(task, mode) {
  const label = task.name;
  let started = false;
  let exitCode = null;
  let error = '';
  const subs = [
    vscode.tasks.onDidStartTaskProcess((e) => {
      if (e.execution.task.name === label) started = true;
    }),
    vscode.tasks.onDidEndTaskProcess((e) => {
      if (e.execution.task.name === label) exitCode = e.exitCode;
    }),
  ];
  const begun = Date.now();
  // executeTask settles long before the inputs resolve -- it is no signal that the task
  // will not start -- so only a rejection ends the drive early.
  vscode.tasks.executeTask(task).then(undefined, (e) => {
    error = String((e && e.message) || e);
  });
  // A task Escape cancelled never starts, and nothing says so: give up on it once it has
  // had as long as the slowest picker takes to draw, plus the terminal's own start.
  const limit = mode === 'escape' ? PLAN.escape_timeout_ms : PLAN.task_timeout_ms;
  let steps = 0;
  try {
    while (!started && !error && Date.now() - begun < limit) {
      await sleep(PLAN.step_ms);
      if (started) break;
      steps += 1;
      if (mode === 'escape') {
        await press(ESCAPE);
      } else {
        await press(FIRST);
        await press(TOGGLE);
        await press(ACCEPT);
      }
    }
    const end = Date.now() + PLAN.settle_ms * 4;
    while (started && exitCode === null && Date.now() < end) await sleep(100);
  } finally {
    subs.forEach((sub) => sub.dispose());
    await press(ESCAPE);
  }
  return {
    label, mode, started, exitCode, error, steps,
    seconds: (Date.now() - begun) / 1000,
  };
}

exports.run = async function run() {
  const commands = new Set(await vscode.commands.getCommands(true));
  const report = {
    missingCommands: [FIRST, TOGGLE, ACCEPT, ESCAPE].filter((c) => !commands.has(c)),
    refusedKeys: refused,
    results: [],
  };
  const write = () => fs.writeFileSync(PLAN.out, JSON.stringify(report, null, 2));
  write();
  const tasks = await vscode.tasks.fetchTasks();
  for (const item of PLAN.tasks) {
    const task = tasks.find((t) => t.name === item.label);
    if (!task) {
      report.results.push({ label: item.label, mode: item.mode, error: 'not found by fetchTasks' });
    } else {
      report.results.push(await drive(task, item.mode));
    }
    write();
  }
};
