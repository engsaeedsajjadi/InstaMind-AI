const BASE = process.env.NEXT_PUBLIC_API_BASE_URL || '/api/v1';
export type ApiOptions = RequestInit & { workspaceId?: string };
export async function api<T>(path:string, options:ApiOptions={}) : Promise<T>{
  const token = typeof window !== 'undefined' ? localStorage.getItem('access_token') : null;
  const workspace = options.workspaceId || (typeof window !== 'undefined' ? localStorage.getItem('workspace_id') : null);
  const headers = new Headers(options.headers);
  if (!headers.has('Content-Type') && options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
  if (token) headers.set('Authorization',`Bearer ${token}`);
  if (workspace) headers.set('X-Workspace-Id',workspace);
  const res=await fetch(`${BASE}${path}`,{...options,headers,cache:'no-store'});
  if(!res.ok){let msg=`HTTP ${res.status}`;try{const e=await res.json();msg=e.detail||e.message||e.title||msg}catch{}throw new Error(msg)}
  if(res.status===204)return undefined as T;
  return res.json();
}
export async function login(email:string,password:string){const data=await api<any>('/auth/login',{method:'POST',body:JSON.stringify({email,password})});localStorage.setItem('access_token',data.access_token);localStorage.setItem('refresh_token',data.refresh_token);return data}
