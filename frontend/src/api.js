export async function submitQuery(queryText) {
  const response = await fetch('/query', {
    method: 'POST', // 👈 Ensure this is NOT on a comment line
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ query: queryText }),
  });
  
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
  }
  
  return await response.json();
}