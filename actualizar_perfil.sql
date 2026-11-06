USE defaultdb;

ALTER TABLE usuarios
  ADD nombre VARCHAR(40) NULL,
  ADD apellido VARCHAR(40) NULL,
  ADD pronombres VARCHAR(30) NULL,
  ADD sexo VARCHAR(10) NULL,
  ADD foto VARCHAR(60) NULL,
  ADD enlaces TEXT NULL;