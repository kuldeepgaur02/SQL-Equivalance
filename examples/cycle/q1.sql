SELECT d.name, count(*) FROM departments d JOIN employees e ON e.dept_id = d.id
JOIN a ON a.id = e.id GROUP BY d.name
