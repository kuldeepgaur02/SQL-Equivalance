-- filter in WHERE: courses whose department is not rich disappear
SELECT c.cid, d.dept_id
FROM courses c LEFT JOIN departments d
  ON c.dept_id = d.dept_id
WHERE d.budget > 100000;
