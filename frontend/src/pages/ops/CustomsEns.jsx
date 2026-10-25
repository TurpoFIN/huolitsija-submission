import React from "react";
import { useParams } from "react-router-dom";
import CustomsFlow from "@/features/customs/ExportFlow";
export default function CustomsEns() { const { id } = useParams(); return <CustomsFlow key={id || "list"} kind="ens" />; }
