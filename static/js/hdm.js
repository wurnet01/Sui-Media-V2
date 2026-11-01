(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const create = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const showStatus = (form, message, kind = 'error') => {
    let status = $('.form-status', form);
    if (!status) {
      status = create('div', 'form-status');
      status.setAttribute('role', 'status');
      form.append(status);
    }
    status.className = 'form-status ' + kind;
    status.textContent = message;
  };
  const csrfToken = () => $('meta[name="csrf-token"]')?.content || '';
  const request = async (form) => {
    const response = await fetch(form.action, {
      method: (form.method || 'POST').toUpperCase(),
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json', 'X-CSRFToken': csrfToken() }
    });
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) throw new Error('The session may have expired. Refresh and try again.');
    const data = await response.json();
    if (!response.ok || data.status === 'error') throw new Error(data.message || 'The action could not be completed.');
    return data;
  };
  const commentNode = (data, reply = false) => {
    const item = create('div', 'comment-item' + (reply ? ' reply-comment' : ''));
    item.dataset.commentId = data.comment_id || '';
    const avatar = create('img', 'profile-picture-small');
    avatar.src = '/uploads/' + (data.avatar || 'default.jpg').split('/').map(encodeURIComponent).join('/');
    avatar.alt = data.username || 'Member';
    const body = create('div', 'comment-body');
    const top = create('div', 'comment-topline');
    top.append(create('strong', '', data.username || 'Member'), create('small', '', data.timestamp || 'Just now'));
    body.append(top, create('p', '', data.content || ''));
    if (!reply) {
      const replyButton = create('button', 'reply-link', 'Reply');
      replyButton.type = 'button';
      body.append(replyButton);
      const replyForm = create('form', 'reply-form');
      replyForm.method = 'post';
      replyForm.action = '/comment/' + (data.post_id || '');
      replyForm.dataset.ajaxForm = 'true';
      const csrf = document.createElement('input'); csrf.type='hidden'; csrf.name='csrf_token'; csrf.value=csrfToken(); replyForm.append(csrf);
      const parent = document.createElement('input');
      parent.type = 'hidden'; parent.name = 'parent_id'; parent.value = data.comment_id;
      const input = document.createElement('input');
      input.type = 'text'; input.name = 'content'; input.className = 'form-control';
      input.placeholder = 'Write a reply…'; input.required = true; input.maxLength = 5000;
      const send = create('button', 'btn btn-sm btn-primary', 'Reply'); send.type = 'submit';
      replyForm.append(parent, input, send);
      body.append(replyForm);
    }
    item.append(avatar, body);
    return item;
  };

  document.addEventListener('click', async (event) => {
    const toggle = event.target.closest('.comment-toggle');
    if (toggle) {
      const section = toggle.closest('.post-card')?.querySelector('.comment-section');
      if (section) section.classList.toggle('open');
      return;
    }
    const reply = event.target.closest('.reply-link');
    if (reply) {
      const form = reply.parentElement.querySelector('.reply-form');
      if (form) { form.classList.toggle('open'); $('input[name="content"]', form)?.focus(); }
      return;
    }
    const storyButton = event.target.closest('.story-entry');
    if (storyButton) {
      const modal = $('#storyViewerModal');
      if (!modal) return;
      const body = $('.story-viewer-body', modal);
      const avatar = $('#storyViewerAvatar');
      const user = $('#storyViewerUser');
      const caption = $('#storyViewerCaption');
      const media = $('#storyViewerMedia');
      avatar.src = storyButton.dataset.storyAvatar || '';
      user.textContent = storyButton.dataset.storyUser || 'HDM member';
      caption.textContent = storyButton.dataset.storyCaption || '';
      media.hidden = true;
      try {
        const response = await fetch('/story/' + encodeURIComponent(storyButton.dataset.storyId) + '/view', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json', 'X-CSRFToken': csrfToken() }
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'ok') throw new Error('This story is no longer available.');
        caption.textContent = data.caption || '';
        avatar.src = data.avatar_url || avatar.src;
        const oldVideo = $('video[data-story-video]', body);
        if (oldVideo) oldVideo.remove();
        if (data.media_type === 'video') {
          media.hidden = true;
          const video = create('video');
          video.dataset.storyVideo = 'true'; video.controls = true; video.autoplay = true; video.playsInline = true;
          const source = create('source'); source.src = data.media_url; video.append(source);
          body.insertBefore(video, caption);
        } else {
          media.src = data.media_url; media.hidden = false;
        }
        const count = $('.story-viewer-meta small', modal);
        if (count) count.textContent = data.views + ' views';
      } catch (error) {
        caption.textContent = error.message;
      }
      return;
    }
    const reaction = event.target.closest('[data-story-reaction]');
    if (reaction) {
      const id = $('#storyViewerModal')?.dataset.storyId;
      if (!id) return;
      const form = new FormData(); form.set('reaction_type', reaction.dataset.storyReaction);
      fetch('/story/' + id + '/react', { method:'POST', body:form, credentials:'same-origin', headers:{'X-Requested-With':'XMLHttpRequest','X-CSRFToken':csrfToken()} })
        .then(r => { if (!r.ok) throw new Error(); reaction.classList.add('active'); })
        .catch(() => {});
    }
  });

  document.addEventListener('show.bs.modal', (event) => {
    if (event.target.id === 'storyViewerModal') {
      const trigger = event.relatedTarget;
      if (trigger?.dataset.storyId) event.target.dataset.storyId = trigger.dataset.storyId;
    }
  });

  document.addEventListener('submit', async (event) => {
    const form = event.target;
    if (!form.matches('[data-ajax-form="true"]')) return;
    event.preventDefault();
    const submit = $('button[type="submit"]', form);
    if (submit?.disabled) return;
    const oldText = submit?.textContent;
    if (submit) submit.disabled = true;
    try {
      const data = await request(form);
      const action = form.action;
      if (action.includes('/like/')) {
        const id = action.match(/\/like\/(\d+)/)?.[1];
        const count = id && $('[data-like-count="' + id + '"]');
        if (count) count.textContent = data.count + ' likes';
        submit?.classList.toggle('active', !!data.liked);
      } else if (action.includes('/save_post/')) {
        submit?.classList.toggle('active', !!data.saved);
        if (submit) submit.textContent = data.saved ? 'Saved' : 'Save';
      } else if (action.includes('/share/')) {
        if (submit) { submit.textContent = 'Shared'; submit.classList.add('active'); }
      } else if (action.includes('/comment/')) {
        const section = form.closest('.comment-section');
        const thread = $('.comment-thread', section || document);
        const isReply = form.classList.contains('reply-form');
        const node = commentNode({ ...data, post_id: action.match(/\/comment\/(\d+)/)?.[1] }, isReply);
        if (isReply) {
          const parent = form.closest('.comment-item');
          parent?.insertAdjacentElement('afterend', node);
        } else if (thread) {
          thread.prepend(node);
          section?.classList.add('open');
        }
        form.reset();
      } else if (action.includes('/story/') && action.includes('/reply')) {
        showStatus(form, data.message || 'Reply sent.', 'success');
        form.reset();
      } else if (action.includes('/send_friend_request/') || action.includes('/accept_friend_request/') || action.includes('/decline_friend_request/')) {
        if (submit) { submit.textContent = data.message || 'Done'; submit.disabled = true; }
        if (form.closest('.request-item')) form.closest('.request-item').remove();
      } else {
        showStatus(form, data.message || 'Done', 'success');
      }
    } catch (error) {
      showStatus(form, error.message || 'Something went wrong.');
      if (submit) submit.textContent = oldText;
    } finally {
      if (submit && submit.textContent === oldText) submit.disabled = false;
    }
  });

  document.addEventListener('change', (event) => {
    if (event.target.matches('#storyMedia, #media')) {
      const label = event.target.closest('label');
      const file = event.target.files?.[0];
      if (label && file) {
        const name = $('.upload-story span', label) || $('span', label);
        if (name) name.textContent = file.name;
      }
    }
  });
})();