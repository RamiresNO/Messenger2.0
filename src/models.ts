export interface User {
  id: string;
  username: string;
  createdAt: string;
}

export interface Conversation {
  id: string;
  title: string;
  participantIds: string[];
  createdAt: string;
}

export interface Message {
  id: string;
  conversationId: string;
  senderId: string;
  text: string;
  createdAt: string;
}

export interface MessengerData {
  users: User[];
  conversations: Conversation[];
  messages: Message[];
}

export function emptyMessengerData(): MessengerData {
  return { users: [], conversations: [], messages: [] };
}
