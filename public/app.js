"use strict";

const elements = {
  accountCreateForm: document.querySelector("#account-create-form"),
  accountDialog: document.querySelector("#account-dialog"),
  accountSelect: document.querySelector("#account-select"),
  accountSelectForm: document.querySelector("#account-select-form"),
  accountSelectSubmit: document.querySelector("#account-select-submit"),
  chatHeader: document.querySelector("#chat-header"),
  chatPanel: document.querySelector("#chat-panel"),
  clearSearch: document.querySelector("#clear-search"),
  conversationCount: document.querySelector("#conversation-count"),
  conversationDialog: document.querySelector("#conversation-dialog"),
  conversationForm: document.querySelector("#conversation-form"),
  conversationList: document.querySelector("#conversation-list"),
  conversationName: document.querySelector("#conversation-name"),
  conversationView: document.querySelector("#conversation-view"),
  messagesArea: document.querySelector("#messages-area"),
  messageForm: document.querySelector("#message-form"),
  messageInput: document.querySelector("#message-input"),
  newChat: document.querySelector("#new-chat"),
  participantList: document.querySelector("#participant-list"),
  profileAvatar: document.querySelector("#profile-avatar"),
  profileName: document.querySelector("#profile-name"),
  searchForm: document.querySelector("#search-form"),
  searchInput: document.querySelector("#search-input"),
  searchResults: document.querySelector("#search-results"),
  searchTitle: document.querySelector("#search-title"),
  searchView: document.querySelector("#search-view"),
  sidebar: document.querySelector(".sidebar"),
  sidebarCreate: document.querySelector("#sidebar-create"),
  sidebarEmpty: document.querySelector("#sidebar-empty"),
  switchUser: document.querySelector("#switch-user"),
  toast: document.querySelector("#toast"),
  usernameInput: document.querySelector("#username-input"),
  welcomeCreate: document.querySelector("#welcome-create"),
  welcomeState: document.querySelector("#welcome-state"),
};

const state = {
  user: null,
  users: [],
  conversations: [],
  activeConversation: null,
  searching: false,
  toastTimer: null,
};

const avatarTones = ["lavender", "peach", "mint", "blue", "rose", "gold"];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || "Не вдалося виконати запит.");
  }
  return result;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(
    () => elements.toast.classList.remove("visible"),
    3600,
  );
}

function createAvatar(label, small = false) {
  const avatar = document.createElement("span");
  avatar.className = `avatar${small ? " avatar-small" : ""}`;
  const initial = label.trim().charAt(0).toLocaleUpperCase() || "?";
  avatar.textContent = initial;
  avatar.setAttribute("aria-hidden", "true");
  const tone = avatarTones[initial.codePointAt(0) % avatarTones.length];
  avatar.classList.add(`avatar-tone-${tone}`);
  return avatar;
}

function getUser(userId) {
  return state.users.find((user) => user.id === userId);
}

function getParticipantNames(conversation) {
  return conversation.participantIds
    .map((userId) => getUser(userId)?.username)
    .filter(Boolean);
}

function formatTime(value) {
  return new Date(value).toLocaleTimeString("uk-UA", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDay(value) {
  const date = new Date(value);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Сьогодні";
  if (date.toDateString() === yesterday.toDateString()) return "Учора";
  return date.toLocaleDateString("uk-UA", {
    day: "numeric",
    month: "long",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

function renderConversations() {
  elements.conversationList.replaceChildren();
  elements.conversationCount.textContent = String(state.conversations.length);
  elements.sidebarEmpty.hidden = state.conversations.length > 0;

  for (const conversation of state.conversations) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "conversation-item";
    item.classList.toggle(
      "active",
      conversation.id === state.activeConversation?.id && !state.searching,
    );
    item.addEventListener("click", () => openConversation(conversation));

    item.append(createAvatar(conversation.title));
    const meta = document.createElement("span");
    meta.className = "conversation-meta";
    const title = document.createElement("span");
    title.className = "conversation-title";
    title.textContent = conversation.title;
    const preview = document.createElement("span");
    preview.className = "conversation-preview";
    const participantCount = conversation.participantIds.length;
    preview.textContent = `${participantCount} ${participantCount === 1 ? "учасник" : participantCount < 5 ? "учасники" : "учасників"}`;
    meta.append(title, preview);
    const time = document.createElement("span");
    time.className = "conversation-time";
    time.textContent = new Date(conversation.createdAt).toLocaleDateString(
      "uk-UA",
      { day: "numeric", month: "short" },
    );
    item.append(meta, time);
    elements.conversationList.append(item);
  }
}

function setCurrentUser(user) {
  state.user = user;
  localStorage.setItem("study-hall-user", user.id);
  elements.profileAvatar.textContent =
    user.username.charAt(0).toLocaleUpperCase();
  elements.profileName.textContent = user.username;
  elements.profileAvatar.className = `avatar avatar-small avatar-tone-${avatarTones[user.username.codePointAt(0) % avatarTones.length]}`;
  state.activeConversation = null;
  state.searching = false;
  elements.searchInput.value = "";
  elements.sidebar.classList.remove("mobile-hidden");
  elements.searchView.hidden = true;
  elements.conversationView.hidden = true;
  elements.welcomeState.hidden = false;
  void loadConversations();
}

async function loadUsers() {
  const result = await api("/api/users");
  state.users = result.users;
}

async function loadConversations() {
  if (!state.user) return;
  try {
    const result = await api(
      `/api/conversations?userId=${encodeURIComponent(state.user.id)}`,
    );
    state.conversations = result.conversations;
    renderConversations();
    if (
      state.activeConversation &&
      !state.conversations.some(
        (conversation) => conversation.id === state.activeConversation.id,
      )
    ) {
      state.activeConversation = null;
      renderMainView();
    }
  } catch (error) {
    showToast(error.message);
  }
}

function renderMainView() {
  const hasConversation = Boolean(state.activeConversation);
  elements.welcomeState.hidden = hasConversation || state.searching;
  elements.conversationView.hidden = !hasConversation || state.searching;
  elements.searchView.hidden = !state.searching;
  if (hasConversation && !state.searching) {
    elements.sidebar.classList.add("mobile-hidden");
  } else {
    elements.sidebar.classList.remove("mobile-hidden");
  }
}

async function openConversation(conversation) {
  state.activeConversation = conversation;
  state.searching = false;
  renderConversations();
  renderMainView();
  renderChatHeader(conversation);
  elements.messagesArea.replaceChildren();
  await refreshMessages(true);
  elements.messageInput.focus();
}

function renderChatHeader(conversation) {
  elements.chatHeader.replaceChildren();
  elements.chatHeader.append(createAvatar(conversation.title));
  const heading = document.createElement("div");
  heading.className = "chat-heading";
  const title = document.createElement("h2");
  title.textContent = conversation.title;
  const subtitle = document.createElement("p");
  subtitle.textContent = getParticipantNames(conversation).join(" · ");
  heading.append(title, subtitle);
  const detail = document.createElement("span");
  detail.className = "header-detail";
  detail.textContent = `${conversation.participantIds.length} учасники`;
  elements.chatHeader.append(heading, detail);
}

function renderMessages(messages, scrollToBottom) {
  const wasNearBottom =
    elements.messagesArea.scrollHeight -
      elements.messagesArea.clientHeight -
      elements.messagesArea.scrollTop <
    80;
  elements.messagesArea.replaceChildren();

  if (messages.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-chat";
    const mark = document.createElement("span");
    mark.className = "empty-chat-mark";
    mark.textContent = "✳";
    const title = document.createElement("h3");
    title.textContent = "Почніть розмову";
    const description = document.createElement("p");
    description.textContent = "Ваше перше повідомлення вже чекає.";
    empty.append(mark, title, description);
    elements.messagesArea.append(empty);
    return;
  }

  let previousDay = "";
  for (const message of messages) {
    const day = new Date(message.createdAt).toDateString();
    if (day !== previousDay) {
      const separator = document.createElement("div");
      separator.className = "message-day";
      separator.textContent = formatDay(message.createdAt);
      elements.messagesArea.append(separator);
      previousDay = day;
    }

    const sender = getUser(message.senderId);
    const isMine = message.senderId === state.user?.id;
    const row = document.createElement("article");
    row.className = `message-row${isMine ? " mine" : ""}`;
    if (!isMine) {
      const avatar = document.createElement("span");
      avatar.className = "message-avatar";
      avatar.textContent = sender?.username.charAt(0).toLocaleUpperCase() || "?";
      avatar.setAttribute("aria-hidden", "true");
      row.append(avatar);
    }

    const content = document.createElement("div");
    content.className = "message-content";
    if (!isMine) {
      const author = document.createElement("p");
      author.className = "message-author";
      author.textContent = sender?.username ?? "Користувач";
      content.append(author);
    }
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = message.text;
    const time = document.createElement("p");
    time.className = "message-time";
    time.textContent = formatTime(message.createdAt);
    content.append(bubble, time);
    row.append(content);
    elements.messagesArea.append(row);
  }

  if (scrollToBottom || wasNearBottom) {
    elements.messagesArea.scrollTop = elements.messagesArea.scrollHeight;
  }
}

async function refreshMessages(scrollToBottom = false) {
  const conversation = state.activeConversation;
  if (!state.user || !conversation || state.searching) return;
  try {
    const result = await api(
      `/api/conversations/${encodeURIComponent(conversation.id)}/messages?userId=${encodeURIComponent(state.user.id)}`,
    );
    if (state.activeConversation?.id === conversation.id) {
      renderMessages(result.messages, scrollToBottom);
    }
  } catch (error) {
    showToast(error.message);
  }
}

function openAccountDialog() {
  const previousValue = state.user?.id ?? "";
  elements.accountSelect.replaceChildren();
  for (const user of state.users) {
    const option = document.createElement("option");
    option.value = user.id;
    option.textContent = user.username;
    option.selected = user.id === previousValue;
    elements.accountSelect.append(option);
  }
  elements.accountSelect.disabled = state.users.length === 0;
  elements.accountSelectSubmit.disabled = state.users.length === 0;
  elements.accountSelectSubmit.classList.toggle(
    "disabled-button",
    state.users.length === 0,
  );
  if (!elements.accountDialog.open) elements.accountDialog.showModal();
}

function openConversationDialog() {
  if (!state.user) {
    openAccountDialog();
    return;
  }
  elements.participantList.replaceChildren();
  const availableUsers = state.users.filter(
    (user) => user.id !== state.user.id,
  );
  if (availableUsers.length === 0) {
    const empty = document.createElement("p");
    empty.className = "participant-empty";
    empty.textContent =
      "Поки що ви тут самі. Попросіть друзів створити профіль на цьому пристрої, щоб запросити їх.";
    elements.participantList.append(empty);
  } else {
    for (const user of availableUsers) {
      const label = document.createElement("label");
      label.className = "participant-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "participant";
      checkbox.value = user.id;
      label.append(checkbox, createAvatar(user.username));
      const username = document.createElement("span");
      username.textContent = user.username;
      label.append(username);
      elements.participantList.append(label);
    }
  }
  elements.conversationName.value = "";
  if (!elements.conversationDialog.open) {
    elements.conversationDialog.showModal();
  }
  elements.conversationName.focus();
}

async function showSearchResults(query) {
  const trimmedQuery = query.trim();
  if (!trimmedQuery) {
    showToast("Введіть слово або фразу для пошуку.");
    return;
  }
  try {
    const params = new URLSearchParams({
      userId: state.user.id,
      q: trimmedQuery,
    });
    const result = await api(`/api/search?${params}`);
    state.searching = true;
    renderMainView();
    elements.searchTitle.textContent = `«${trimmedQuery}» · ${result.messages.length} ${result.messages.length === 1 ? "результат" : "результатів"}`;
    elements.searchResults.replaceChildren();

    if (result.messages.length === 0) {
      const empty = document.createElement("div");
      empty.className = "no-results";
      const title = document.createElement("strong");
      title.textContent = "Нічого не знайдено";
      const description = document.createElement("span");
      description.textContent = "Спробуйте інший запит або коротше слово.";
      empty.append(title, description);
      elements.searchResults.append(empty);
    } else {
      for (const message of result.messages) {
        const conversation = state.conversations.find(
          (item) => item.id === message.conversationId,
        );
        const button = document.createElement("button");
        button.type = "button";
        button.className = "search-result";
        const meta = document.createElement("span");
        meta.className = "search-result-meta";
        const title = document.createElement("span");
        title.className = "search-result-title";
        title.textContent = conversation?.title ?? "Розмова";
        const details = document.createElement("span");
        const sender = getUser(message.senderId);
        details.textContent = `${sender?.username ?? "Користувач"} · ${new Date(message.createdAt).toLocaleString("uk-UA")}`;
        meta.append(title, details);
        const text = document.createElement("span");
        text.className = "search-result-text";
        text.textContent = message.text;
        button.append(meta, text);
        button.addEventListener("click", () => {
          const target = state.conversations.find(
            (item) => item.id === message.conversationId,
          );
          if (target) void openConversation(target);
        });
        elements.searchResults.append(button);
      }
    }
  } catch (error) {
    showToast(error.message);
  }
}

elements.accountSelectForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const user = getUser(elements.accountSelect.value);
  if (!user) {
    showToast("Спочатку створіть профіль.");
    return;
  }
  elements.accountDialog.close();
  setCurrentUser(user);
});

elements.accountCreateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = elements.usernameInput.value.trim();
  try {
    const result = await api("/api/users", {
      method: "POST",
      body: JSON.stringify({ username }),
    });
    state.users.push(result.user);
    elements.accountDialog.close();
    elements.usernameInput.value = "";
    setCurrentUser(result.user);
  } catch (error) {
    showToast(error.message);
  }
});

elements.conversationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const selectedIds = Array.from(
    elements.participantList.querySelectorAll(
      'input[name="participant"]:checked',
    ),
  ).map((checkbox) => checkbox.value);
  try {
    const result = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({
        title: elements.conversationName.value,
        participantIds: [state.user.id, ...selectedIds],
      }),
    });
    elements.conversationDialog.close();
    await loadConversations();
    await openConversation(result.conversation);
  } catch (error) {
    showToast(error.message);
  }
});

elements.messageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = elements.messageInput.value;
  if (!text.trim() || !state.user || !state.activeConversation) return;
  try {
    await api(
      `/api/conversations/${encodeURIComponent(state.activeConversation.id)}/messages`,
      {
        method: "POST",
        body: JSON.stringify({ senderId: state.user.id, text }),
      },
    );
    elements.messageInput.value = "";
    elements.messageInput.style.height = "auto";
    await refreshMessages(true);
  } catch (error) {
    showToast(error.message);
  }
});

elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.messageForm.requestSubmit();
  }
});

elements.messageInput.addEventListener("input", () => {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 130)}px`;
});

elements.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.user) void showSearchResults(elements.searchInput.value);
});

elements.clearSearch.addEventListener("click", () => {
  state.searching = false;
  elements.searchInput.value = "";
  renderMainView();
  renderConversations();
});

elements.switchUser.addEventListener("click", openAccountDialog);
elements.newChat.addEventListener("click", openConversationDialog);
elements.sidebarCreate.addEventListener("click", openConversationDialog);
elements.welcomeCreate.addEventListener("click", openConversationDialog);

for (const button of document.querySelectorAll("[data-close-dialog]")) {
  button.addEventListener("click", () => {
    const dialog = button.closest("dialog");
    if (state.user && dialog) dialog.close();
  });
}

elements.accountDialog.addEventListener("cancel", (event) => {
  if (!state.user) event.preventDefault();
});

elements.accountDialog.addEventListener("click", (event) => {
  if (event.target === elements.accountDialog && state.user) {
    elements.accountDialog.close();
  }
});

elements.conversationDialog.addEventListener("click", (event) => {
  if (event.target === elements.conversationDialog) {
    elements.conversationDialog.close();
  }
});

async function start() {
  try {
    await loadUsers();
    const savedUser = state.users.find(
      (user) => user.id === localStorage.getItem("study-hall-user"),
    );
    if (savedUser) {
      setCurrentUser(savedUser);
    } else {
      localStorage.removeItem("study-hall-user");
      openAccountDialog();
    }
  } catch (error) {
    showToast(error.message);
  }
}

setInterval(() => {
  if (state.activeConversation && !state.searching) {
    void refreshMessages();
  }
}, 4000);

void start();
