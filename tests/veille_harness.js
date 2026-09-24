// Exécute un script de `actions/github-script` hors de GitHub, contre un faux
// GitHub et une horloge réglable, et imprime les actions qu'il a tentées.
//
//   node veille_harness.js <script.js> <scenario.json>
//
// Le script est celui du fichier YAML, tel quel : c'est lui qu'on éprouve, pas
// une copie. github-script l'exécute dans une fonction asynchrone qui reçoit
// `github`, `context`, `core` et `require` ; on fait exactement pareil.

const fs = require("fs");

const [cheminScript, cheminScenario] = process.argv.slice(2);
const script = fs.readFileSync(cheminScript, "utf8");
const sc = JSON.parse(fs.readFileSync(cheminScenario, "utf8"));

const actions = [];
Date.now = () => sc.maintenant;

const introuvable = () => Object.assign(new Error("Not Found"), { status: 404 });

const github = {
  rest: {
    actions: {
      listWorkflowRuns: async () => ({
        data: {
          workflow_runs: sc.derniere_reussite
            ? [{ created_at: sc.derniere_reussite, updated_at: sc.derniere_reussite }]
            : [],
        },
      }),
    },
    issues: {
      listForRepo: async ({ labels }) => ({
        data: (sc.ouvertes || []).filter(i => (i.labels || []).includes(labels)),
      }),
      listComments: async () => ({ data: sc.commentaires || [] }),
      create: async p => { actions.push({ quoi: "ouvre", ...p }); return { data: { number: 99 } }; },
      createComment: async p => { actions.push({ quoi: "commente", ...p }); },
      update: async p => { actions.push({ quoi: "modifie", ...p }); },
    },
    repos: {
      get: async () => ({ data: { default_branch: "main" } }),
      getBranch: async () => ({
        data: { commit: { commit: { committer: { date: sc.dernier_commit } } } },
      }),
      getContent: async () => {
        if (sc.fichier_sha) return { data: { sha: sc.fichier_sha } };
        throw introuvable();
      },
      createOrUpdateFileContents: async p => { actions.push({ quoi: "commit", ...p }); },
    },
  },
};

const context = {
  repo: { owner: "proprio", repo: "depot" },
  payload: sc.message ? { head_commit: { message: sc.message } } : {},
};

const core = {
  info: () => {},
  warning: () => {},
  setFailed: m => actions.push({ quoi: "échoue", message: m }),
};

const Asynchrone = Object.getPrototypeOf(async function () {}).constructor;
new Asynchrone("github", "context", "core", "require", script)(github, context, core, require)
  .then(() => process.stdout.write(JSON.stringify(actions)))
  .catch(erreur => { process.stderr.write(String(erreur.stack || erreur)); process.exit(2); });
