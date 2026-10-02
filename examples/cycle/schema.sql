-- departments <-> employees: a two-table FK cycle (one FK nullable);
-- employees.mentor_id: nullable self-reference;
-- nodes.parent_id: NOT NULL self-reference (the row points at itself);
-- a <-> b: a cycle where every FK is NOT NULL (deferred);
-- archive: not read by the queries.
CREATE TABLE departments (id int PRIMARY KEY, name text NOT NULL, manager_id int);
CREATE TABLE employees (
    id        int PRIMARY KEY,
    dept_id   int NOT NULL REFERENCES departments(id),
    mentor_id int REFERENCES employees(id),
    node_id   int,
    salary    int NOT NULL CHECK (salary BETWEEN 1000 AND 9000)
);
ALTER TABLE departments ADD FOREIGN KEY (manager_id) REFERENCES employees(id);
CREATE TABLE nodes (id int PRIMARY KEY, parent_id int NOT NULL REFERENCES nodes(id));
ALTER TABLE employees ADD FOREIGN KEY (node_id) REFERENCES nodes(id);
CREATE TABLE a (id int PRIMARY KEY, b_id int NOT NULL);
CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL REFERENCES a(id));
ALTER TABLE a ADD FOREIGN KEY (b_id) REFERENCES b(id);
CREATE TABLE archive (id int PRIMARY KEY, employee_id int REFERENCES employees(id));
