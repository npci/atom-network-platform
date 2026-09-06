export interface User {
  id: string;
  name: string;
}

export class UserService {
  private users: User[] = [];

  add(user: User): void {
    this.users.push(user);
  }

  find(id: string): User | undefined {
    return this.users.find((u) => u.id === id);
  }
}

export function formatName(u: User): string {
  return u.name.trim();
}
