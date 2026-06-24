import { computed, ref } from 'vue';
import Preview from './Preview.js';
import NotesChannel from './NotesChannel.js';
import SelectChannel from './SelectChannel.js';
import { postJSON } from '../util.js';

// The right pane: everything about the selected artifact — a large preview, its keys/state/
// deps/timestamp, output ports (as /file links), feedback channels (reusing the channel
// components), and actions: run-this-cell, reroll, generate-more. Channels mutate the shared
// instance objects, so edits reflect in the list immediately.
export default {
  components: { Preview, NotesChannel, SelectChannel },
  props: {
    row: { type: Object, default: null },
    bust: { type: Number, required: true },
  },
  emits: ['run', 'flagchange'],
  setup(props, { emit }) {
    const busy = ref('');

    // A minimal card object the channel components key their POSTs by.
    const card = computed(() => (props.row ? { id: props.row.cardId } : null));
    const keyPairs = computed(() =>
      props.row ? Object.entries(props.row.inst.keys) : []);

    function fileUrl(rel) {
      return `/file?path=${encodeURIComponent(rel)}`;
    }

    function runCell() {
      emit('run', { only: [props.row.rule], where: props.row.inst.keys });
    }
    async function reroll() {
      busy.value = 'reroll';
      try {
        await postJSON('/api/reroll', { id: props.row.cardId });
      } catch { /* surfaced by the run log */ }
      busy.value = '';
      runCell();
    }
    async function generate() {
      busy.value = 'gen';
      try {
        await postJSON('/api/trigger', { rule: props.row.rule });
        emit('run');
      } finally {
        busy.value = '';
      }
    }

    return { card, keyPairs, busy, fileUrl, runCell, reroll, generate };
  },
  template: `
    <div v-if="!row" class="empty">Select an artifact.</div>
    <div v-else class="detail">
      <div class="dethead">
        <span class="detid">{{ row.identity }}</span>
        <span class="badge" :class="'b-' + row.state" :title="row.reason">{{ row.state }}</span>
      </div>

      <div class="detpreview">
        <Preview v-if="row.artifact_url" :card="row" :bust="bust" />
        <div v-else class="none">{{ row.inst.outputs.length ? '📁 scatter output (a directory)' : 'no output yet' }}</div>
      </div>

      <div class="detacts">
        <button type="button" @click="runCell">▶ Run this cell</button>
        <button v-if="row.reroll" type="button" :disabled="busy==='reroll'" @click="reroll">🎲 Re-roll</button>
        <button v-if="row.trigger" class="gen" type="button"
                :disabled="busy==='gen'" @click="generate">✦ Generate more</button>
      </div>

      <table class="proptable">
        <tr><th>rule</th><td>{{ row.rule }}</td></tr>
        <tr v-for="[k, v] in keyPairs" :key="k"><th>{{ k }}</th><td>{{ v }}</td></tr>
        <tr><th>state</th><td>{{ row.state }} — {{ row.reason }}</td></tr>
        <tr v-if="row.deps && row.deps.length"><th>deps</th><td>{{ row.deps.join(', ') }}</td></tr>
        <tr v-if="row.completed_at"><th>updated</th><td>{{ row.completed_at }}</td></tr>
      </table>

      <div class="detsection" v-if="row.inst.outputs.length">
        <h3>outputs</h3>
        <ul class="outlist">
          <li v-for="o in row.inst.outputs" :key="o.port">
            <span class="port">{{ o.port }}</span>
            <a v-if="o.exists && !o.is_dir" :href="fileUrl(o.rel)" target="_blank">{{ o.rel }}</a>
            <span v-else class="dim">{{ o.rel }}{{ o.is_dir ? '/' : '' }}{{ o.exists ? '' : ' (missing)' }}</span>
          </li>
        </ul>
      </div>

      <div class="detsection channels" v-if="row.inst.channels.length">
        <h3>feedback</h3>
        <component v-for="ch in row.inst.channels" :key="ch.name"
                   :is="ch.kind === 'select' ? 'SelectChannel' : 'NotesChannel'"
                   :card="card" :channel="ch" @flagchange="$emit('flagchange')" />
      </div>
    </div>
  `,
};
