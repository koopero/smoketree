import { ref } from 'vue';
import { postJSON } from '../util.js';

// Write-only append box: submit explicitly (button or ⌘/Ctrl+Enter), then clear.
// The note text is never read back; only `has_note` flips for the flag highlight.
export default {
  props: {
    card: { type: Object, required: true },
    channel: { type: Object, required: true },
  },
  emits: ['flagchange'],
  setup(props, { emit }) {
    const value = ref('');
    const busy = ref(false);
    const saved = ref('');

    async function add() {
      if (!value.value.trim() || busy.value) return;
      busy.value = true;
      try {
        const j = await postJSON('/api/note', {
          id: props.card.id,
          channel: props.channel.name,
          text: value.value,
        });
        value.value = '';
        props.channel.has_note = j.has_note;
        emit('flagchange');
        saved.value = 'added';
        setTimeout(() => { saved.value = ''; }, 1500);
      } finally {
        busy.value = false;
      }
    }

    function onKeydown(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        add();
      }
    }

    return { value, busy, saved, add, onKeydown };
  },
  template: `
    <div class="channel">
      <div class="chead">{{ channel.name }}<template v-if="channel.describe"> — <span class="desc">{{ channel.describe }}</span></template></div>
      <textarea placeholder="Add a note…" v-model="value" @keydown="onKeydown"></textarea>
      <div class="noterow">
        <button class="addnote" type="button" :disabled="busy" @click="add">Add note</button>
        <span class="saved">{{ saved }}</span><span class="hint">⌘/Ctrl + Enter</span>
      </div>
    </div>
  `,
};
